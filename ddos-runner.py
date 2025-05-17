import os
import subprocess
import time

# --- Configuration ---
# NUM_TERMINALS = 3  # Number of "virtual" terminals (tmux sessions) to open # Commented out or removed
TMUX_SESSION_BASENAME = "ddos_session" # Base name for tmux sessions
# --- End Configuration ---

def main():
    # Get the absolute path of the directory where this script is located
    try:
        script_dir = os.path.abspath(os.path.dirname(__file__))
    except NameError:
        # Fallback if __file__ is not defined (e.g., in an interactive interpreter)
        script_dir = os.path.abspath(os.getcwd())

    # Define paths relative to the script directory
    venv_activate_path = os.path.join(script_dir, "myenv/bin/activate")
    ddos_dir = os.path.join(script_dir, "ddos")
    ddos_script_path = os.path.join(ddos_dir, "ddos-hard.py")

    # --- Path Validations ---
    if not os.path.exists(venv_activate_path):
        print(f"Error: Virtual environment activate script not found at: {venv_activate_path}")
        print("Please ensure 'myenv/bin/activate' exists relative to the script.")
        return
    if not os.path.isdir(ddos_dir):
        print(f"Error: 'ddos' directory not found at: {ddos_dir}")
        print("Please ensure 'ddos' directory exists relative to the script.")
        return
    if not os.path.exists(ddos_script_path):
        print(f"Error: 'ddos-hard.py' script not found in: {ddos_dir}")
        print("Please ensure 'ddos-hard.py' exists within the 'ddos' directory.")
        return
# Check if tmux is installed
    try:
        subprocess.run(["tmux", "-V"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: tmux does not seem to be installed or is not in PATH.")
        print("Please install tmux (e.g., 'sudo apt install tmux') and try again.")
        return

    # Ask user for the number of terminals
    while True:
        try:
            num_terminals_str = input("Enter the number of tmux sessions to create: ")
            num_terminals = int(num_terminals_str)
            if num_terminals > 0:
                break
            else:
                print("Please enter a positive integer.")
        except ValueError:
            print("Invalid input. Please enter a number.")

    # Command to be executed inside each tmux session.
    # This command sources the venv, changes directory, runs the python script,
    # and then starts an interactive bash session to keep the terminal open.
    command_for_session = (
        f"echo 'Activating venv: {venv_activate_path}'; "
        f"source '{venv_activate_path}' && "
        f"echo 'Changing to DDoS directory: {ddos_dir}'; "
        f"cd '{ddos_dir}' && "
        f"echo 'Running ddos-hard.py in tmux session (pwd: $(pwd))...'; "
        f"python ddos-hard.py; "
        f"DDOS_EXIT_CODE=$?; "
        f"echo 'ddos-hard.py finished with exit code: $DDOS_EXIT_CODE. Press Ctrl+B then D to detach, or type exit to close this pane.'; "
        f"exec bash"  # Keeps the session alive with a root shell
    )
   # Determine if the script is running as root
    is_running_as_root = os.geteuid() == 0

    print(f"Preparing to launch {num_terminals} tmux sessions.")
    if is_running_as_root:
        print("Script is running as root. Tmux sessions will run commands as root.")
    else:
        print("Script is not running as root. Tmux sessions will attempt 'sudo su'.")
        print("This might require password input if not configured for passwordless sudo.")
        print("Password prompts inside detached tmux sessions can be problematic.")

    print(f"Script directory: {script_dir}")
    print(f"Venv activate: {venv_activate_path}")
    print(f"DDoS directory: {ddos_dir}")
    print(f"DDoS script: {ddos_script_path}")
    print("-" * 30)

    for i in range(num_terminals):
        session_name = f"{TMUX_SESSION_BASENAME}_{i}"
        print(f"Creating tmux session {session_name} ({i + 1}/{num_terminals})...")

        if is_running_as_root:
            # If script is root, tmux runs as root, and command_for_session runs as root.
            # No need for `sudo su -c` wrapper.
            final_command_for_tmux = command_for_session
        else:
            # If script is not root, we need to elevate privileges inside tmux.
            # This uses double quotes around command_for_session to handle internal single quotes correctly.
            final_command_for_tmux = f"sudo su -c \"{command_for_session}\""

        tmux_command = [
            "tmux", "new-session", "-d", "-s", session_name,
            final_command_for_tmux
        ]
# print(f"Executing: {' '.join(tmux_command)}") # Uncomment for debugging
        try:
            # Capture stderr and stdout for better error reporting if check=True fails
            result = subprocess.run(tmux_command, check=True, capture_output=True, text=True)
            if result.stdout:
                 print(f"tmux stdout for {session_name}: {result.stdout}")
            if result.stderr: # Should be empty on success for new-session -d
                 print(f"tmux stderr for {session_name}: {result.stderr}")
        except subprocess.CalledProcessError as e:
            print(f"Error creating tmux session {session_name}: {e}")
            print(f"Command: {' '.join(e.cmd)}")
            print(f"Return code: {e.returncode}")
            print(f"Stderr: {e.stderr if e.stderr else 'N/A'}")
            print(f"Stdout: {e.stdout if e.stdout else 'N/A'}")
            # Optionally, decide if you want to stop or continue
            # return
        except FileNotFoundError:
            print("Error: tmux command not found. Please ensure it's installed and in PATH.")
            return
        except Exception as e:
            print(f"An unexpected error occurred with tmux session {session_name}: {e}")
            # return

        time.sleep(0.2) # Brief pause

    print("-" * 30)
    print(f"Successfully attempted to launch {num_terminals} tmux session(s).")
    if not is_running_as_root:
        print("If sessions require sudo, password prompts might occur inside tmux and may not be visible if detached.")
    
    print("\nTo list sessions: tmux ls")
    print(f"To attach to a session (e.g., the first one): tmux attach -t {TMUX_SESSION_BASENAME}_0")
    print("Inside tmux: Ctrl+B then D to detach. Type 'exit' to close shells/panes.")
    print("If 'tmux ls' shows 'no server running' or sessions are missing, check for errors above or inside tmux logs if available.")
    print("Exiting script. Enjoy your DDoS testing responsibly!")
    
    if __name__ == "__main__":
        main()
