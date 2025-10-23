#!/usr/bin/env python3
import os
import pty
import select
import time
import sys
import tty
import termios
import signal
import struct
import fcntl
import re
import logging
import argparse

log = logging.getLogger(__name__)


class SessionRecorder:
    def __init__(self, output_file="recorded_session.txt", silence_threshold=0.5):
        self.output_file = output_file
        self.silence_threshold = silence_threshold
        self.child_pid = None
        self.prompt_map = {
            '$ ': 'sh',
            'mysql> ': 'mysql',
            '>>> ': 'python',
        }

    def record_session(self):
        """Record an interactive session and generate command file"""
        print(f"Type commands as normal. Session will be recorded to {self.output_file}.")
        print("Press Ctrl+T to stop recording, Ctrl+C goes to shell.\n")

        master, slave = pty.openpty()

        # Set parent's stdin to raw mode to detect individual keystrokes
        old_stdin_settings = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())

        # Set stdin to non-blocking mode
        stdin_flags = fcntl.fcntl(sys.stdin.fileno(), fcntl.F_GETFL)
        fcntl.fcntl(sys.stdin.fileno(), fcntl.F_SETFL, stdin_flags | os.O_NONBLOCK)

        pid = os.fork()

        if pid == 0:
            # Child process - proper terminal setup
            os.close(master)
            os.setsid()  # Create new session for proper terminal control
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.close(slave)

            # Start shell with proper terminal setup
            os.environ['TERM'] = 'xterm'
            os.environ['PS1'] = '$ '

            os.execlp('bash', 'bash', '--norc', '-i')
        else:
            # Parent process - store child PID for signal forwarding
            self.child_pid = pid

            # Set up signal handler to forward SIGINT to child
            def forward_sigint(signum, frame):
                print("\n[Forwarding Ctrl+C to child shell]")
                try:
                    os.kill(pid, signal.SIGINT)
                except ProcessLookupError:
                    pass

            signal.signal(signal.SIGINT, forward_sigint)

            os.close(slave)

            # Local variables for recording session
            recorded_commands = []  # Commands to be written to output file
            last_output_time = time.time()  # Last time shell emitted output
            current_prompt = ""  # Current shell prompt (everything since last newline)
            last_recorded_prompt = ""  # Last prompt sent to recording
            command_so_far = ""  # Current command being typed by user
            recording = True  # True until we should stop recording

            try:
                while recording:
                    log.debug("Calling select")
                    r, w, e = select.select([master, sys.stdin], [], [], min(self.silence_threshold, 10))

                    # Check for output silence
                    time_since_output = time.time() - last_output_time
                    output_is_silent = time_since_output >= self.silence_threshold

                    child_exited = False

                    for fd in r:
                        if fd == master:
                            # Output from child process
                            try:
                                log.debug("Reading from master")
                                data = os.read(master, 1024)
                                if not data:
                                    log.debug("EOF from master - child exited")
                                    child_exited = True
                                    break

                                # Write to stdout so user can see
                                log.debug("Writing to stdout")
                                os.write(1, data)

                                # Update output tracking
                                last_output_time = time.time()

                                # Process output for prompt detection
                                decoded_data = data.decode('utf-8', errors='ignore')

                                # Update current prompt: everything since last newline
                                if '\n' in decoded_data:
                                    # Newline found - reset prompt to content after last newline
                                    lines = decoded_data.split('\n')
                                    current_prompt = lines[-1]  # Content after last newline
                                else:
                                    # No newline - append to current prompt
                                    current_prompt += decoded_data
                                log.debug("Current prompt updated to: %r", current_prompt)

                            except (OSError, IOError):
                                log.debug("Error reading from master - child likely exited")
                                child_exited = True
                                break

                        elif fd == sys.stdin:
                            # Input from user - character-oriented non-blocking read
                            # print('@@ Reading from stdin (character-oriented)')
                            try:
                                user_char_just_read = sys.stdin.read(1)
                                log.debug("User input: %r", user_char_just_read)

                                # Handle EOF (empty string from read)
                                if not user_char_just_read:
                                    log.debug("EOF on stdin - stopping recording")
                                    recording = False
                                    break

                                # Check for Ctrl+T (ASCII 20) to stop recording
                                if user_char_just_read == '\x14':  # Ctrl+T
                                    log.debug("Ctrl+T detected - stopping recording")
                                    recording = False
                                    break

                                # Send to child process (unless it's Ctrl+T)
                                if user_char_just_read != '\x14':
                                    log.debug("Writing user_char_just_read=%r to master", user_char_just_read)
                                    os.write(master, user_char_just_read.encode())

                                # Check if we're at a prompt - start command when we get user input
                                log.debug(
                                    "Command state: output_is_silent=%s, current_command=%r",
                                    output_is_silent,
                                    command_so_far,
                                )

                                # Check if user is typing after silence period
                                clean_prompt = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', current_prompt)
                                if output_is_silent and clean_prompt and clean_prompt != last_recorded_prompt:
                                    log.debug("Recording prompt: %r", clean_prompt)
                                    # Record prompt before first user input after silence
                                    # Map common prompts to shorter forms
                                    prompt_command = self.prompt_map.get(clean_prompt, f"prompt {clean_prompt}")
                                    recorded_commands.append(f"#> {prompt_command}")
                                    last_recorded_prompt = clean_prompt

                                # Check for command completion (Enter key)
                                if user_char_just_read in ['\r', '\n']:
                                    log.debug("Command completion detected. Command: %r", command_so_far)
                                    # Save the completed command
                                    if command_so_far:
                                        log.debug("Recording command")
                                        recorded_commands.append(command_so_far)
                                        log.debug("RECORDED: %r", command_so_far)
                                        command_so_far = ""
                                    continue

                                # Ok, just a normal key
                                command_so_far += user_char_just_read
                                log.debug("Command is now: %r", command_so_far)

                            except IOError:
                                # Expected when no data available in non-blocking mode
                                log.debug("No data available (non-blocking)")
                                pass

                    # Check if we need to break out of main loop
                    if child_exited:
                        recording = False

            except Exception as e:
                print(f"\nUnexpected error: {e}")
            finally:
                # Restore signal handling
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                # Restore parent's stdin settings
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_stdin_settings)
                # Write final commands and cleanup
                self.recorded_commands = recorded_commands
                self.finalize_recording()
                os.close(master)
                try:
                    os.kill(self.child_pid, signal.SIGTERM)
                    os.waitpid(self.child_pid, 0)
                except (ChildProcessError, ProcessLookupError):
                    pass

    def finalize_recording(self):
        """Open file and write all recorded commands at the end"""
        if self.recorded_commands:
            print(f"\nRecorded {len(self.recorded_commands)} commands to {self.output_file}")
            with open(self.output_file, 'w') as f:
                f.write("# Automatically recorded session\n")
                f.write("# Commands will be replayed according to prompts\n")
                f.write("# See https://github.com/CapnKernel/termit for details\n")
                for command in self.recorded_commands:
                    f.write(f"{command}\n")
        else:
            print("\nNo commands recorded")


def main():
    parser = argparse.ArgumentParser(description='Record terminal session commands')
    parser.add_argument(
        'output_file',
        nargs='?',
        default='recorded_session.txt',
        help='Output file for recorded commands (default: recorded_session.txt)',
    )
    parser.add_argument(
        'silence_threshold',
        nargs='?',
        type=float,
        default=2.0,
        help='Silence threshold in seconds for prompt detection (default: 2.0)',
    )
    parser.add_argument('--debug', action='store_true', help='Enable debug output')

    args = parser.parse_args()

    # Configure logging based on debug flag
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
    else:
        logging.basicConfig(level=logging.WARNING, format='%(message)s')

    recorder = SessionRecorder(args.output_file, args.silence_threshold)
    recorder.record_session()


if __name__ == "__main__":
    main()
