import subprocess
import logging

logger = logging.getLogger(__name__)

class SystemUtils:
    @staticmethod
    def execute_command(command: list) -> tuple[bool, str]:
        """Executes a shell command and returns success status and output."""
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            return True, result.stdout
        except subprocess.CalledProcessError as e:
            error_message = f"Command failed with exit code {e.returncode}:\n{e.stderr}"
            logger.error(f"System command error: {error_message}")
            return False, error_message
        except FileNotFoundError:
            return False, "Error: The required system command (e.g., docker) was not found."

    @staticmethod
    def start_container(image_name: str, container_name: str = None, ports: list[tuple[int, int]] = None) -> tuple[bool, str]:
        """Starts a Docker container from a specified image."""
        command = ["docker", "run", "-d"]
        args = []

        if container_name:
            command.append("--name")
            command.append(container_name)
        
        # Basic port mapping (assuming host:container format for simplicity)
        port_mapping = ""
        if ports:
            for host, container in ports:
                port_mapping += f"-p {host}:{container}/tcp "

        # For MongoDB specifically, we might need to ensure it runs with proper volume/credentials
        if image_name.lower() == "mongo":
             command.extend(["-v", "/data/mongodb:/data"]) # Example volume mapping
             command.append(image_name)
        else:
            command.append(image_name)

        # Append port mappings if they exist (this logic is simplified for the patch example)
        if port_mapping:
            # Note: In a real implementation, we'd build the command list dynamically
            pass 

        logger.info(f"Attempting to start container with command: {' '.join(command)}")
        success, output = SystemUtils.execute_command(command)
        return success, f"Container operation successful. Output:\n{output}"

# Ensure this class is initialized or imported correctly in the main application flow.
