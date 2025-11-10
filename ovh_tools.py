#!/usr/bin/env python3
"""
OVH VPS management tool.

This script provides management of OVH VPS instances with modes for:
- SSH key creation and upload
- VPS installation with SSH key and inventory update (automatically finds OS image, can be called multiple times)
- Add VPS to inventory

Usage:
    # First time setup - create and upload SSH key (no passphrase)
    uv run python ovh_tools.py ssh-key create [--key-name ayb-deploy]

    # Create SSH key with passphrase
    uv run python ovh_tools.py ssh-key create --key-name ayb-deploy --passphrase "your-passphrase"

    # Force recreate existing SSH key
    uv run python ovh_tools.py ssh-key create --key-name ayb-deploy --force

    # Install VPS with OS name (automatically matches to image ID)
    uv run python ovh_tools.py install --service-name vps-abc.vps.ovh.net --os "Ubuntu 24.04"

    # Add VPS to inventory (without installing)
    uv run python ovh_tools.py add-to-inventory --service-name vps-abc.vps.ovh.net
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import ovh
import yaml


# ============================================================================
# Shared Utility Functions
# ============================================================================

def decrypt_secrets(secrets_file="secrets.yaml"):
    """Decrypt secrets.yaml using SOPS."""
    age_key_file = os.environ.get("SOPS_AGE_KEY_FILE")
    if not age_key_file:
        print("Warning: SOPS_AGE_KEY_FILE environment variable not set.", file=sys.stderr)
        print("SOPS will look for keys in default locations:", file=sys.stderr)
        print("  - ~/.config/sops/age/keys.txt", file=sys.stderr)
        print("  - $XDG_CONFIG_HOME/sops/age/keys.txt", file=sys.stderr)
        print("", file=sys.stderr)

    try:
        env = os.environ.copy()
        result = subprocess.run(
            ["sops", "-d", secrets_file],
            capture_output=True,
            text=True,
            check=True,
            env=env
        )
        return yaml.safe_load(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"Error decrypting secrets: {e.stderr}", file=sys.stderr)
        print("\nMake sure SOPS_AGE_KEY_FILE is set and points to your age private key:", file=sys.stderr)
        print("  export SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("Error: sops command not found. Please install SOPS.", file=sys.stderr)
        print("  macOS: brew install sops", file=sys.stderr)
        print("  Linux: see https://github.com/getsops/sops/releases", file=sys.stderr)
        sys.exit(1)


def create_ovh_client(secrets):
    """Initialize OVH client with credentials from secrets."""
    ovh_config = secrets.get("ovh", {})

    required_keys = ["application_key", "application_secret", "consumer_key"]
    missing_keys = [key for key in required_keys if key not in ovh_config]
    if missing_keys:
        print(f"Error: Missing required OVH credentials in secrets: {', '.join(missing_keys)}", file=sys.stderr)
        sys.exit(1)

    endpoint = ovh_config.get("endpoint", "ovh-us")

    return ovh.Client(
        endpoint=endpoint,
        application_key=ovh_config["application_key"],
        application_secret=ovh_config["application_secret"],
        consumer_key=ovh_config["consumer_key"],
    )


# ============================================================================
# SSH Key Management
# ============================================================================

def check_local_ssh_key(key_name):
    """Check if SSH key exists locally in ~/.ssh/"""
    ssh_dir = Path.home() / ".ssh"
    private_key = ssh_dir / key_name
    public_key = ssh_dir / f"{key_name}.pub"
    return private_key.exists() and public_key.exists()


def check_remote_ssh_key(client, key_name):
    """Check if SSH key exists on OVH account."""
    try:
        keys = client.get("/me/sshKey")
        return key_name in keys
    except Exception as e:
        print(f"Error checking remote SSH keys: {e}", file=sys.stderr)
        return False


def validate_ssh_key(client, key_name):
    """Validate SSH key exists locally and on OVH. Fail fast if missing."""
    # Check local
    if not check_local_ssh_key(key_name):
        print(f"Error: SSH key '{key_name}' not found locally in ~/.ssh/", file=sys.stderr)
        print(f"Run: uv run python ovh_tools.py ssh-key create --key-name {key_name}", file=sys.stderr)
        sys.exit(1)

    # Check remote
    if not check_remote_ssh_key(client, key_name):
        print(f"Error: SSH key '{key_name}' not found on OVH account", file=sys.stderr)
        print(f"Run: uv run python ovh_tools.py ssh-key create --key-name {key_name}", file=sys.stderr)
        sys.exit(1)

    print(f"✓ SSH key '{key_name}' validated")


def generate_ssh_key(key_name, passphrase="", force=False):
    """Generate SSH key pair using ssh-keygen."""
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)

    key_path = ssh_dir / key_name
    public_key_path = ssh_dir / f"{key_name}.pub"

    if check_local_ssh_key(key_name):
        if not force:
            print(f"✓ SSH key '{key_name}' already exists locally")
            return
        else:
            print(f"⚠️  Overwriting existing SSH key '{key_name}'")
            # Delete existing key files to avoid ssh-keygen prompting for overwrite
            if key_path.exists():
                key_path.unlink()
            if public_key_path.exists():
                public_key_path.unlink()

    passphrase_msg = "with passphrase" if passphrase else "without passphrase"
    print(f"Generating SSH key pair {passphrase_msg}: {key_path}...")
    try:
        subprocess.run(
            [
                "ssh-keygen",
                "-t", "ed25519",
                "-f", str(key_path),
                "-N", passphrase,
                "-C", f"ayb-deploy-key-{key_name}"
            ],
            check=True,
            capture_output=True
        )
        print(f"✓ SSH key generated at {key_path}")
    except subprocess.CalledProcessError as e:
        print(f"Error generating SSH key: {e.stderr.decode()}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("Error: ssh-keygen command not found", file=sys.stderr)
        sys.exit(1)


def upload_ssh_key(client, key_name, force=False):
    """Upload SSH public key to OVH account."""
    ssh_dir = Path.home() / ".ssh"
    public_key_path = ssh_dir / f"{key_name}.pub"

    if not public_key_path.exists():
        print(f"Error: Public key not found at {public_key_path}", file=sys.stderr)
        sys.exit(1)

    # Check if already uploaded
    if check_remote_ssh_key(client, key_name):
        if not force:
            print(f"✓ SSH key '{key_name}' already uploaded to OVH")
            return
        else:
            print(f"⚠️  Deleting existing SSH key '{key_name}' from OVH")
            try:
                client.delete(f"/me/sshKey/{key_name}")
                print(f"✓ Existing SSH key deleted")
            except Exception as e:
                print(f"Error deleting existing SSH key: {e}", file=sys.stderr)
                sys.exit(1)

    # Read public key content
    public_key_content = public_key_path.read_text().strip()

    print(f"Uploading SSH key '{key_name}' to OVH...")
    try:
        client.post("/me/sshKey",
            keyName=key_name,
            key=public_key_content
        )
        print(f"✓ SSH key '{key_name}' uploaded to OVH")
    except Exception as e:
        print(f"Error uploading SSH key: {e}", file=sys.stderr)
        sys.exit(1)


# ============================================================================
# VPS Operations
# ============================================================================

def get_vps_details(client, service_name):
    """Retrieve VPS details including IP address."""
    print(f"\nRetrieving details for {service_name}...")

    vps = client.get(f"/vps/{service_name}")
    ips = client.get(f"/vps/{service_name}/ips")

    # Separate IPv4 and IPv6 addresses
    ipv4_addresses = [ip for ip in ips if ':' not in ip]
    ipv6_addresses = [ip for ip in ips if ':' in ip]

    # Prefer IPv4 for primary IP
    primary_ip = ipv4_addresses[0] if ipv4_addresses else (ipv6_addresses[0] if ipv6_addresses else None)

    details = {
        "serviceName": service_name,
        "name": vps.get("name", service_name),
        "displayName": vps.get("displayName", service_name),
        "state": vps.get("state"),
        "zone": vps.get("zone"),
        "model": vps.get("model", {}).get("name"),
        "memory": vps.get("memoryLimit"),
        "ip": primary_ip,
        "ipv4": ipv4_addresses[0] if ipv4_addresses else None,
        "ipv6": ipv6_addresses[0] if ipv6_addresses else None,
        "ips": ips,
    }

    print(f"VPS Details:")
    print(f"  Service Name: {details['serviceName']}")
    print(f"  State: {details['state']}")
    print(f"  Zone: {details['zone']}")
    print(f"  Memory: {details['memory']} MB")
    if details['ipv4']:
        print(f"  IPv4: {details['ipv4']}")
    if details['ipv6']:
        print(f"  IPv6: {details['ipv6']}")

    return details


def list_available_images(client, service_name):
    """List available OS images for a VPS."""
    try:
        # The correct OVH API endpoint is /vps/{serviceName}/images/available
        image_ids = client.get(f"/vps/{service_name}/images/available")

        # Get details for each image
        images = []
        for image_id in image_ids:
            try:
                image_info = client.get(f"/vps/{service_name}/images/available/{image_id}")
                images.append({
                    "id": image_id,
                    "name": image_info.get("name"),
                    "description": image_info.get("description"),
                    "family": image_info.get("family")
                })
            except Exception as e:
                # If we can't get details, just add the ID
                images.append({"id": image_id, "name": image_id})

        return images
    except Exception as e:
        print(f"Error listing available images: {e}", file=sys.stderr)
        return []


def find_image_by_name(client, service_name, os_name):
    """Find an image ID by matching natural language OS name."""
    print(f"Fetching available images for {service_name}...")
    images = list_available_images(client, service_name)

    if not images:
        print("Error: Could not fetch available images", file=sys.stderr)
        return None

    # Normalize search term
    os_name_lower = os_name.lower()

    # Try exact match first (in case they provide the image ID directly)
    for image in images:
        if image['id'] == os_name:
            print(f"✓ Using image ID: {image['id']}")
            return image['id']

    # Try matching by name or description
    matches = []
    for image in images:
        name = (image.get('name') or '').lower()
        description = (image.get('description') or '').lower()

        # Check if the search term appears in name or description
        if os_name_lower in name or os_name_lower in description:
            matches.append(image)

    if len(matches) == 0:
        print(f"\nError: No images found matching '{os_name}'", file=sys.stderr)
        print("\nAvailable images:", file=sys.stderr)
        for image in images:
            print(f"  - {image.get('name', image['id'])}", file=sys.stderr)
            if image.get('description'):
                print(f"    {image['description']}", file=sys.stderr)
        return None

    if len(matches) == 1:
        print(f"✓ Found match: {matches[0].get('name', matches[0]['id'])}")
        print(f"  Using image ID: {matches[0]['id']}")
        return matches[0]['id']

    # Multiple matches - show options
    print(f"\nMultiple images match '{os_name}':", file=sys.stderr)
    for i, image in enumerate(matches, 1):
        print(f"  {i}. {image.get('name', image['id'])}", file=sys.stderr)
        if image.get('description'):
            print(f"     {image['description']}", file=sys.stderr)
        print(f"     ID: {image['id']}", file=sys.stderr)

    print("\nPlease be more specific or use the exact image ID with --os", file=sys.stderr)
    return None


def install_vps_with_ssh_key(client, service_name, image_id, ssh_key_name):
    """Install VPS with SSH key pre-installed using the correct OVH API endpoint.

    Note: This destructively rebuilds the VPS, erasing all existing data.
    Can be called multiple times to reinstall the VPS with a fresh OS.
    """
    print(f"\nInstalling VPS with image ID '{image_id}' and SSH key '{ssh_key_name}'...")

    # Read the public key content from local file
    ssh_dir = Path.home() / ".ssh"
    public_key_path = ssh_dir / f"{ssh_key_name}.pub"

    if not public_key_path.exists():
        print(f"Error: Public key not found at {public_key_path}", file=sys.stderr)
        return False

    public_key_content = public_key_path.read_text().strip()
    print(f"Read public key from {public_key_path}")

    try:
        # Build request body according to vps.rebuild.post schema
        # Use publicSshKey (actual key content) instead of sshKey (key name reference)
        # because sshKey doesn't work for VPS rebuilds
        rebuild_body = {
            "imageId": image_id,
            "publicSshKey": public_key_content,
            "doNotSendPassword": True,  # Don't email password since we're using SSH key
        }

        print(f"Calling rebuild with publicSshKey and doNotSendPassword=True")

        # Make the API call with request body
        result = client.post(f"/vps/{service_name}/rebuild", **rebuild_body)

        print(f"API Response: {result}")

        task_id = result.get("id")  # Task ID is returned as 'id' not 'taskId'
        print(f"Rebuild started (task {task_id})")

        # Wait for rebuild to complete
        print("\nWaiting for rebuild to complete...")
        max_wait = 600  # 10 minutes
        poll_interval = 10
        start_time = time.time()

        while time.time() - start_time < max_wait:
            try:
                # Get task status
                task = client.get(f"/vps/{service_name}/tasks/{task_id}")
                state = task.get("state")

                print(f"Rebuild status: {state}")

                if state == "done":
                    print("\n✓ VPS rebuild complete")
                    print(f"\n✓ SSH key '{ssh_key_name}' pre-installed")
                    print(f"\nConnect with:")
                    print(f"  ssh -i ~/.ssh/{ssh_key_name} ubuntu@{service_name}")
                    return True
                elif state in ["cancelled", "error"]:
                    print(f"Error: VPS rebuild {state}", file=sys.stderr)
                    return False
            except Exception as e:
                print(f"Error checking rebuild status: {e}", file=sys.stderr)

            time.sleep(poll_interval)

        print(f"Timeout waiting for VPS rebuild after {max_wait}s", file=sys.stderr)
        return False

    except Exception as e:
        print(f"\nError rebuilding VPS: {e}", file=sys.stderr)
        print(f"Error type: {type(e).__name__}", file=sys.stderr)

        # Try to get more details from the exception
        if hasattr(e, 'response'):
            print(f"Response status: {e.response.status_code if hasattr(e.response, 'status_code') else 'N/A'}", file=sys.stderr)
            print(f"Response body: {e.response.text if hasattr(e.response, 'text') else e.response}", file=sys.stderr)

        # Show available images on error
        print("\nFetching available OS images...", file=sys.stderr)
        images = list_available_images(client, service_name)
        if images:
            print(f"\nAvailable images for {service_name}:", file=sys.stderr)
            for image in images:
                print(f"  - ID: {image['id']}", file=sys.stderr)
                if image.get('name'):
                    print(f"    Name: {image['name']}", file=sys.stderr)
                if image.get('description'):
                    print(f"    Description: {image['description']}", file=sys.stderr)
            print(f"\nTry running with: --os \"<image_id>\"", file=sys.stderr)

        return False


# ============================================================================
# Inventory Management
# ============================================================================

def update_vps_inventory(vps_details, inventory_file="vps_instances.json"):
    """Add or update VPS details in vps_instances.json."""
    inventory_path = Path(inventory_file)

    if inventory_path.exists():
        instances = json.loads(inventory_path.read_text())
    else:
        instances = []

    new_entry = {
        "ip": vps_details["ip"],
        "serviceName": vps_details["serviceName"],
        "name": vps_details["name"],
        "displayName": vps_details["displayName"],
        "zone": vps_details["zone"],
        "model": vps_details["model"],
        "memory": vps_details["memory"],
        "state": vps_details["state"],
        "ips": vps_details["ips"],
    }

    # Check if VPS already exists in inventory
    existing_index = None
    for i, instance in enumerate(instances):
        if instance.get("serviceName") == vps_details["serviceName"]:
            existing_index = i
            break

    if existing_index is not None:
        # Update existing entry
        instances[existing_index] = new_entry
        print(f"\nVPS instance updated in inventory: {inventory_path}")
    else:
        # Add new entry
        instances.append(new_entry)
        print(f"\nVPS instance added to inventory: {inventory_path}")

    inventory_path.write_text(json.dumps(instances, indent=2))
    print(f"  Total instances: {len(instances)}")


# ============================================================================
# Command Mode Implementations
# ============================================================================

def cmd_ssh_key_create(args):
    """ssh-key create mode: Generate SSH key locally and upload to OVH."""
    print("=" * 60)
    print(f"SSH Key Setup: {args.key_name}")
    print("=" * 60)

    # Generate key locally if needed
    passphrase = args.passphrase if args.passphrase else ""
    force = args.force if hasattr(args, 'force') and args.force else False
    generate_ssh_key(args.key_name, passphrase, force)

    # Decrypt secrets and create client
    print("\nDecrypting secrets...")
    secrets = decrypt_secrets(args.secrets)

    print("Initializing OVH client...")
    client = create_ovh_client(secrets)

    # Upload key to OVH if needed
    upload_ssh_key(client, args.key_name, force)

    print("\n✓ SSH key setup complete!")
    print(f"Key location: ~/.ssh/{args.key_name}")


def cmd_add_to_inventory(args):
    """add-to-inventory mode: Fetch VPS details and add/update inventory."""
    print("=" * 60)
    print(f"Adding VPS to Inventory: {args.service_name}")
    print("=" * 60)

    # Decrypt secrets
    print("\nDecrypting secrets...")
    secrets = decrypt_secrets(args.secrets)

    # Create OVH client
    print("Initializing OVH client...")
    client = create_ovh_client(secrets)

    # Get VPS details
    vps_details = get_vps_details(client, args.service_name)

    # Update inventory
    update_vps_inventory(vps_details, args.vps_instances)

    print(f"\n✓ VPS added/updated in inventory!")
    print(f"\nTo connect: ssh ubuntu@{args.service_name}")


def cmd_install(args):
    """install mode: Install VPS with SSH key (destructively rebuilds, can be called multiple times)."""
    print("=" * 60)
    print(f"Installing VPS: {args.service_name}")
    print("=" * 60)

    # Decrypt secrets
    print("\nDecrypting secrets...")
    secrets = decrypt_secrets(args.secrets)

    # Create OVH client
    print("Initializing OVH client...")
    client = create_ovh_client(secrets)

    # Validate SSH key
    print(f"\nValidating SSH key '{args.ssh_key_name}'...")
    validate_ssh_key(client, args.ssh_key_name)

    # Find image ID from OS name
    print(f"\nLooking up OS image: '{args.os}'")
    image_id = find_image_by_name(client, args.service_name, args.os)

    if not image_id:
        print("\n✗ Could not find matching OS image", file=sys.stderr)
        sys.exit(1)

    # Install VPS
    rebuild_success = install_vps_with_ssh_key(
        client,
        args.service_name,
        image_id,
        args.ssh_key_name
    )

    if rebuild_success:
        print("\n✓ VPS install complete!")

        # Get updated VPS details
        vps_details = get_vps_details(client, args.service_name)

        # Update inventory
        update_vps_inventory(vps_details, args.vps_instances)

        print(f"\n✓ SSH key '{args.ssh_key_name}' pre-installed")
        print(f"\nConnect with:")
        print(f"  ssh -i ~/.ssh/{args.ssh_key_name} ubuntu@{args.service_name}")
    else:
        print("\n✗ VPS install failed", file=sys.stderr)
        print("\nSuggestion: Install via OVH web interface, then run:", file=sys.stderr)
        print(f"  uv run python ovh_tools.py add-to-inventory --service-name {args.service_name}", file=sys.stderr)
        sys.exit(1)


# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="OVH VPS management tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # First time - create and upload SSH key (no passphrase)
  %(prog)s ssh-key create

  # Create SSH key with passphrase
  %(prog)s ssh-key create --passphrase "your-passphrase"

  # Force recreate existing SSH key
  %(prog)s ssh-key create --force

  # Install VPS with OS name (automatically finds matching image, can be called multiple times)
  %(prog)s install --service-name vps-abc.vps.ovh.net --os "Ubuntu 24.04"

  # Add VPS to inventory (without installing/rebuilding)
  %(prog)s add-to-inventory --service-name vps-abc.vps.ovh.net
"""
    )

    subparsers = parser.add_subparsers(dest="mode", required=True, help="Operation mode")

    # ========================================================================
    # ssh-key mode
    # ========================================================================
    ssh_parser = subparsers.add_parser("ssh-key", help="Manage SSH keys")
    ssh_subparsers = ssh_parser.add_subparsers(dest="ssh_action", required=True)

    # ssh-key create
    ssh_create = ssh_subparsers.add_parser("create", help="Create and upload SSH key")
    ssh_create.add_argument("--key-name", default="ayb-deploy", help="SSH key name (default: ayb-deploy)")
    ssh_create.add_argument("--passphrase", help="Passphrase for the SSH key (optional, defaults to no passphrase)")
    ssh_create.add_argument("--force", action="store_true", help="Force overwrite existing SSH key locally and on OVH")
    ssh_create.add_argument("--secrets", default="secrets.yaml", help="Path to SOPS-encrypted secrets file")

    # ========================================================================
    # add-to-inventory mode
    # ========================================================================
    inventory_parser = subparsers.add_parser("add-to-inventory", help="Fetch VPS details and add/update inventory")
    inventory_parser.add_argument("--service-name", required=True, help="VPS service name (e.g., vps-abc.vps.ovh.net)")
    inventory_parser.add_argument("--secrets", default="secrets.yaml", help="Path to SOPS-encrypted secrets file")
    inventory_parser.add_argument("--vps-instances", default="vps_instances.json", help="Path to VPS instances JSON file")

    # ========================================================================
    # install mode
    # ========================================================================
    install_parser = subparsers.add_parser("install", help="Install VPS with SSH key and update inventory (destructively rebuilds, can be called multiple times)")
    install_parser.add_argument("--service-name", required=True, help="VPS service name (e.g., vps-abc.vps.ovh.net)")
    install_parser.add_argument("--os", required=True, help='OS name or image ID (e.g., "Ubuntu 24.04", "Debian 12")')
    install_parser.add_argument("--ssh-key-name", default="ayb-deploy", help="SSH key name (default: ayb-deploy)")
    install_parser.add_argument("--secrets", default="secrets.yaml", help="Path to SOPS-encrypted secrets file")
    install_parser.add_argument("--vps-instances", default="vps_instances.json", help="Path to VPS instances JSON file")

    args = parser.parse_args()

    # Route to appropriate command handler
    if args.mode == "ssh-key":
        if args.ssh_action == "create":
            cmd_ssh_key_create(args)
    elif args.mode == "add-to-inventory":
        cmd_add_to_inventory(args)
    elif args.mode == "install":
        cmd_install(args)


if __name__ == "__main__":
    main()
