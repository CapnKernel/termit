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


class SessionRecorder:
    def __init__(self, output_file="recorded_session.txt", silence_threshold=0.5):
        self.output_file = output_file
        self.silence_threshold = silence_threshold
        self.last_output_time = time.time()
        self.output_active = False
        self.recorded_commands = []
        self.child_pid = None

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

            current_command = ""

            try:
                while True:
                    r, w, e = select.select([master, sys.stdin], [], [], 10.0)

                    # Check for output silence
                    time_since_output = time.time() - self.last_output_time
                    output_is_silent = time_since_output >= self.silence_threshold

                    child_exited = False

                    for fd in r:
                        if fd == master:
                            # Output from child process
                            try:
                                data = os.read(master, 1024)
                                if not data:
                                    child_exited = True
                                    break

                                # Write to stdout so user can see
                                os.write(1, data)

                                # Update output tracking
                                self.last_output_time = time.time()
                                self.output_active = True

                            except (OSError, IOError):
                                child_exited = True
                                break

                        elif fd == sys.stdin:
                            # Input from user - character-oriented non-blocking read
                            try:
                                user_char_just_read = sys.stdin.read(1)

                                # Handle EOF (empty string from read)
                                if not user_char_just_read:
                                    break

                                # Check for Ctrl+T (ASCII 20) to stop recording
                                if user_char_just_read == '\x14':  # Ctrl+T
                                    break

                                # Send to child process (unless it's Ctrl+T)
                                if user_char_just_read != '\x14':
                                    os.write(master, user_char_just_read.encode())

                                # Check if we're at a prompt - start command when we get user input

                                # Start a new command if we're not already in one
                                if not current_command:
                                    current_command = user_char_just_read
                                else:
                                    # Continuation of existing command
                                    current_command += user_char_just_read

                                # Check for command completion (Enter key)
                                if user_char_just_read in ['\r', '\n']:
                                    if current_command.strip():
                                        # Save the completed command
                                        command = current_command.rstrip('\r\n')
                                        if command:
                                            self.recorded_commands.append(command)

                                        current_command = ""
                            except IOError:
                                # Expected when no data available in non-blocking mode
                                pass

                    # Check if we need to break out of main loop
                    if child_exited:
                        break

                    # Check for exit conditions from stdin
                    if not r and output_is_silent and self.output_active:
                        # No activity and output is silent - might be at prompt
                        self.output_active = False
            except Exception as e:
                print(f"\nUnexpected error: {e}")
            finally:
                # Restore signal handling
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                # Restore parent's stdin settings
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_stdin_settings)
                # Write final commands and cleanup
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
                f.write("# Commands will be replayed with timing based on user input\n")
                for command in self.recorded_commands:
                    f.write(f"{command}\n")
        else:
            print("\nNo commands recorded")


def main():
    output_file = sys.argv[1] if len(sys.argv) > 1 else "recorded_session.txt"
    silence_threshold = float(sys.argv[2]) if len(sys.argv) > 2 else 10

    recorder = SessionRecorder(output_file, silence_threshold)
    recorder.record_session()


if __name__ == "__main__":
    main()
