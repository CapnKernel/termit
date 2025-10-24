#!/usr/bin/env python3
import os
import pty
import select
import time
import sys
import tty
import termios
import re
import logging
import argparse

log = logging.getLogger(__name__)


class PTYRunner:
    def __init__(self, silence_threshold=0.5):
        self.current_prompt = r'\$ '  # Default shell prompt
        self.prompt_patterns = {
            'sh': [r'\$ ', r'# ', r'> '],
            'mysql': [r'mysql> ', r'-> ', r'"\'> '],
            'psql': [r'[#=>] ', r'-\? '],
            'python': [r'>>> ', r'\.\.\. '],
        }
        self.silence_threshold = silence_threshold

    def wait_for_prompt(self, master, timeout=30):
        """Wait for any of the current prompt patterns to appear"""
        log.debug(
            "wait_for_prompt: timeout=%s, current_prompt=%s, silence_threshold=%s",
            timeout,
            self.current_prompt,
            self.silence_threshold,
        )

        buffer = b''
        end_time = time.time() + timeout
        last_output_time = time.time()
        prompt_found = False

        while time.time() < end_time:
            r, w, e = select.select([master], [], [], min(self.silence_threshold, 10))
            if r:
                data = os.read(master, 1024)
                log.debug("Received %d bytes from PTY: %r", len(data), data)
                os.write(1, data)  # Echo to stdout
                buffer += data
                last_output_time = time.time()  # Reset silence timer on any data
                prompt_found = False  # Reset prompt found flag on new data

                # Check for prompt patterns in the buffer
                text = buffer.decode('utf-8', errors='ignore')
                log.debug("Buffer text: %r", text)

                # Check if we see prompt text in the buffer
                if self._check_prompt(self.current_prompt, text):
                    log.debug("Found prompt text: %s", self.current_prompt)
                    prompt_found = True

                # Keep only recent data to avoid buffer growing too large
                if len(buffer) > 1000:
                    buffer = buffer[-500:]

            # Check if we found prompt text and have had silence for threshold period
            time_since_output = time.time() - last_output_time
            output_is_silent = time_since_output >= self.silence_threshold

            if prompt_found and output_is_silent:
                log.debug("Prompt confirmed after silence period of %.2f seconds", time_since_output)
                return True

        log.warning("Timeout waiting for prompt: %s", self.current_prompt)
        print(f"\nTimeout waiting for prompt: {self.current_prompt}")
        return False

    def _check_prompt(self, prompts, text):
        """Check if prompt pattern matches the text"""
        # Strip ANSI escape sequences from text
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        clean_text = ansi_escape.sub('', text)

        # Look for prompt at the end of the text
        if isinstance(prompts, list):
            # Multiple prompt patterns
            if any((re.search(pattern + r'$', clean_text, re.MULTILINE) for pattern in prompts)):
                return True
        else:
            # Single prompt pattern
            if re.search(prompts + r'$', clean_text, re.MULTILINE):
                return True
        return False

    def send_command(self, master, command):
        """Send a command to the PTY"""
        log.debug("Sending command: %r", command)
        os.write(master, f"{command}\r".encode())

    def send_eof(self, master):
        """Send Ctrl+D (EOF)"""
        log.debug("Sending EOF (Ctrl+D)")
        os.write(master, b'\x04')  # Ctrl+D

    def send_control(self, master, control_char):
        """Send control character"""
        # control_char should be like 'c' for Ctrl+C, 'd' for Ctrl+D, etc.
        log.debug("Sending control character: Ctrl+%s", control_char.upper())
        char_code = ord(control_char.upper()) - 64
        os.write(master, bytes([char_code]))

    def process_command_file(self, filename):
        """Process commands from a text file"""
        log.debug("Processing command file: %s", filename)

        try:
            with open(filename, 'r') as f:
                commands = [line.strip() for line in f if line.strip() and not line.startswith('#')]
            log.debug("Loaded %d commands: %s", len(commands), commands)
        except FileNotFoundError:
            print(f"Error: Command file '{filename}' not found")
            return

        # Start PTY
        master, slave = pty.openpty()
        log.debug("Created PTY: master=%d, slave=%d", master, slave)

        # Set the slave PTY to raw mode
        old_settings = termios.tcgetattr(slave)
        tty.setraw(slave)

        pid = os.fork()
        log.debug("Forked process: pid=%d", pid)

        if pid == 0:
            # Child process
            log.debug("Child process starting bash shell")
            os.close(master)
            os.setsid()
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.close(slave)

            # Start interactive shell
            os.execlp('bash', 'bash', '--norc', '-i')
        else:
            # Parent process
            os.close(slave)

            try:
                # Wait for initial prompt
                log.debug("Waiting for initial prompt")
                if not self.wait_for_prompt(master):
                    print("Failed to get initial prompt")
                    return

                # Process each command from file
                for i, line in enumerate(commands):
                    log.debug("Processing command %d/%d: %r", i + 1, len(commands), line)

                    if line.startswith('#>'):
                        # This is a meta-command
                        self._process_meta_command(master, line[2:].strip())
                    else:
                        # This is a regular command to execute
                        print(f"\033[1;32m$\033[0m {line}", end="")  # Show command in green without newline
                        self.send_command(master, line)
                        if not self.wait_for_prompt(master):
                            print("Command failed to complete")
                            break
                        time.sleep(0.5)  # Brief pause between commands

                # Keep reading any remaining output
                print("\n--- Session complete, waiting for any final output ---")
                time.sleep(2)

                # Try to read any remaining data
                try:
                    while True:
                        r, w, e = select.select([master], [], [], 1.0)
                        if r:
                            data = os.read(master, 1024)
                            if not data:
                                log.debug("No more data from PTY")
                                break
                            os.write(1, data)
                        else:
                            log.debug("Timeout waiting for more data")
                            break
                except (OSError, IOError) as e:
                    log.debug("Error reading remaining data: %s", e)

            except KeyboardInterrupt:
                print("\nInterrupted by user")
            finally:
                log.debug("Closing PTY master and waiting for child process")
                os.close(master)
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass

    def _process_meta_command(self, master, meta_cmd):
        """Process a meta-command starting with #>"""
        log.debug("Processing meta-command: %r", meta_cmd)

        parts = meta_cmd.split(' ', 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == 'prompt':
            if arg:
                self.current_prompt = arg
                log.debug("Set prompt pattern to: %s", arg)
                print(f"\033[1;34m[Setting prompt pattern: {arg}]\033[0m")
            else:
                print("\033[1;31m[Error: prompt command requires argument]\033[0m")

        elif cmd == 'sh':
            self.current_prompt = self.prompt_patterns['sh']
            log.debug("Set prompt to shell patterns")
            print("\033[1;34m[Setting prompt to shell patterns]\033[0m")

        elif cmd == 'mysql':
            self.current_prompt = self.prompt_patterns['mysql']
            log.debug("Set prompt to MySQL patterns")
            print("\033[1;34m[Setting prompt to MySQL patterns]\033[0m")

        elif cmd == 'python':
            self.current_prompt = self.prompt_patterns['python']
            log.debug("Set prompt to Python patterns")
            print("\033[1;34m[Setting prompt to Python patterns]\033[0m")

        elif cmd == 'psql':
            self.current_prompt = self.prompt_patterns['psql']
            log.debug("Set prompt to PostgreSQL patterns")
            print("\033[1;34m[Setting prompt to PostgreSQL patterns]\033[0m")

        elif cmd == 'eof':
            print("\033[1;34m[Sending EOF (Ctrl+D)]\033[0m")
            self.send_eof(master)
            time.sleep(0.5)

        elif cmd == 'sleep':
            try:
                seconds = float(arg)
                log.debug("Sleeping for %s seconds", seconds)
                print(f"\033[1;34m[Sleeping for {seconds} seconds]\033[0m")
                time.sleep(seconds)
            except ValueError:
                print("\033[1;31m[Error: sleep command requires numeric argument]\033[0m")

        elif cmd == 'interact':
            print("\033[1;34m[Entering interactive mode - type 'exit' to continue]\033[0m")
            self._interactive_mode(master)

        elif cmd == 'comment':
            print(f"\033[1;36m[Note: {arg}]\033[0m")

        else:
            print(f"\033[1;31m[Unknown meta-command: {cmd}]\033[0m")

    def _interactive_mode(self, master):
        """Allow user to type commands manually"""
        import select

        print("Interactive mode - type commands directly, or 'exit' to resume script")

        while True:
            r, w, e = select.select([master, sys.stdin], [], [])

            for fd in r:
                if fd == master:
                    # Output from child process
                    data = os.read(master, 1024)
                    if not data:
                        return
                    os.write(1, data)
                elif fd == sys.stdin:
                    # Input from user
                    user_input = sys.stdin.readline()
                    if user_input.strip().lower() == 'exit':
                        return
                    os.write(master, user_input.encode())


def main():
    parser = argparse.ArgumentParser(description='Replay terminal session commands')
    parser.add_argument('command_file', help='Command file to replay')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    parser.add_argument(
        '--silence-threshold',
        type=float,
        default=0.5,
        help='Silence threshold in seconds for prompt detection (default: 0.5)',
    )

    args = parser.parse_args()

    # Configure logging based on debug flag
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
    else:
        logging.basicConfig(level=logging.WARNING, format='%(message)s')

    runner = PTYRunner(silence_threshold=args.silence_threshold)
    runner.process_command_file(args.command_file)


if __name__ == "__main__":
    main()
