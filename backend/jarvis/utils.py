import subprocess
import os

def run_command(command):
    """Helper function to execute shell commands."""
    print(f"Executing command: {' '.join(command)}")
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        return result.stdout
    except subprocess.CalledProcessError as e:
        print(f"Error executing command: {e}")
        print(f"Stdout: {e.stdout}")
        print(f"Stderr: {e.stderr}")
        return None
    except FileNotFoundError:
        print("Error: Command not found. Is Docker installed and in PATH?")
        return None

def start_mongodb_docker():
    """Starts a MongoDB container using the official Docker image."""
    print("Attempting to start MongoDB Docker Container...")
    # Check if container already exists and stop/remove it first for clean run
    run_command(["docker", "stop", "mongodb"])
    run_command(["docker", "rm", "mongodb"])

    # Run the new container in detached mode
    container_name = "mongodb"
    image_name = "mongo:latest"
    command = ["docker", "run", "-d", "--name", container_name, image_name]

    output = run_command(command)

    if output and "Container created" in output or output:
        print("\n✅ MongoDB Docker Container started successfully!")
        print("You can connect using: docker exec -it mongodb mongosh")
        return True
    else:
        print("\n❌ Failed to start the MongoDB container. Please check if Docker is running and accessible.")
        return False
