import subprocess
import time
import os

# -----------------------------------------------------------------
# --- CONFIGURATION ---
# -----------------------------------------------------------------

# Set this to the full path of your server executable.
# IMPORTANT: Use double backslashes (\\) for Windows paths.
SERVER_EXE_PATH = r".\\Speechify.exe"

# Set this to the server's working directory.
# This is usually the same directory as the .exe file.
# The server needs to run here to find its config and voice files.
SERVER_WORKING_DIR = r".\\"

# -----------------------------------------------------------------

# --- SCRIPT START ---

def run_server_watchdog():
    # Simple check to make sure the file exists before we start
    if not os.path.exists(SERVER_EXE_PATH):
        print(f"--- WATCHDOG ERROR ---")
        print(f"Server executable not found at:")
        print(f"{SERVER_EXE_PATH}")
        print("Please update the SERVER_EXE_PATH variable in this script.")
        input("Press Enter to exit...")
        return

    if not os.path.isdir(SERVER_WORKING_DIR):
        print(f"--- WATCHDOG ERROR ---")
        print(f"Server working directory not found at:")
        print(f"{SERVER_WORKING_DIR}")
        print("Please update the SERVER_WORKING_DIR variable in this script.")
        input("Press Enter to exit...")
        return

    print(f"--- Server Watchdog Active ---")
    print(f"Target:      {SERVER_EXE_PATH}")
    print(f"Working Dir: {SERVER_WORKING_DIR}")
    print("\nPress Ctrl+C in this window to stop the watchdog.")

    try:
        while True:
            print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting SpfyTom8.exe...")

            # Start the server process.
            # We use Popen so Python doesn't wait for it to finish (yet).
            # 'cwd' (Current Working Directory) is critical so the server
            # can find its config files and voice (tom.vin, tom8.vdb).
            process = subprocess.Popen([SERVER_EXE_PATH], cwd=SERVER_WORKING_DIR)

            # Now, we wait for the process to terminate (which it will
            # do after handling one request).
            process.wait()

            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Server process exited (code {process.returncode}).")
            print("Restarting in 1 second...")
            time.sleep(1) # Short delay to prevent runaway loops if it crashes instantly

    except KeyboardInterrupt:
        print("\nWatchdog stopped by user (Ctrl+C).")
        # Try to terminate the child process if it's still running
        if 'process' in locals() and process.poll() is None:
            print("Terminating running server process...")
            process.terminate()
        print("Exiting.")

    except Exception as e:
        print(f"\n--- WATCHDOG CRITICAL ERROR ---")
        print(f"An unhandled error occurred: {e}")
        print("Watchdog is stopping.")
        if 'process' in locals() and process.poll() is None:
            process.terminate()

if __name__ == "__main__":
    run_server_watchdog()
