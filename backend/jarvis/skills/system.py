import subprocess
import os
import platform
from typing import Optional

# --- System Utility Functions ---

def run_command(cmd: list[str], check=True) -> tuple[int, str]:
    """Executes a shell command and returns the return code and output."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=check)
        return result.returncode, result.stdout + result.stderr
    except FileNotFoundError:
        return 127, f"Error: Command not found. Is the necessary tool installed and in PATH? ({' '.join(cmd)})"
    except subprocess.CalledProcessError as e:
        return e.returncode, f"Command failed with error:\n{e.stderr}"

def check_docker_connection() -> bool:
    """Checks if Docker is running and accessible."""
    # On Windows, checking connectivity can be tricky due to named pipes.
    # We attempt a simple 'docker info' command which requires the daemon to be active.
    try:
        print("Attempting to connect to Docker daemon...")
        # Use subprocess directly for better control over execution context
        result = subprocess.run(
            ["docker", "info"], 
            capture_output=True, text=True, check=False
        )
        if result.returncode == 0 and "Client: Docker Engine - Community" in result.stdout:
            print("Successfully connected to Docker daemon.")
            return True
        elif "Cannot connect to the Docker daemon" in result.stderr or "No such file or directory" in result.stderr:
             # This handles common connection failures gracefully
            print("Warning: Could not connect to Docker daemon. Is Docker Desktop running?")
            return False
        else:
            # Catch other non-zero exit codes
            print(f"Error connecting to Docker daemon (Exit Code {result.returncode}). Output:\n{result.stderr}")
            return False

    except FileNotFoundError:
        print("Error: 'docker' command not found. Please ensure Docker is installed and in your system PATH.")
        return False
    except Exception as e:
        print(f"An unexpected error occurred while checking Docker connection: {e}")
        return False


def execute_container_command(command: list[str], container_name: Optional[str] = None) -> str:
    """Executes a docker command (e.g., run, start, stop)."""
    if not check_docker_connection():
        return "Error: Docker daemon is unavailable or connection failed. Please ensure Docker Desktop is running."

    full_command = ["docker"] + command
    print(f"Executing command: docker {' '.join(command)}")
    
    try:
        result = subprocess.run(
            full_command, 
            capture_output=True, text=True, check=True
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        return f"Docker command failed (Exit Code {e.returncode}):\n{e.stderr}"
    except Exception as e:
        return f"An unexpected error occurred during Docker execution: {e}"

# --- End System Utility Functions ---
