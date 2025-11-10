# ayb Deployment

This directory contains a template for how to deploy `ayb` to a server
(a VPS on OVH, in this case). It uses SOPS/age for secrets management,
and Kamal to deploy docker containers.

## Setup

### 1. Install dependencies

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create and activate virtual environment
uv venv
source .venv/bin/activate

# Install Python dependencies
uv pip install -r requirements.txt
```

### 2. Install SOPS and Age

```bash
# macOS
brew install sops age

# Linux
# See https://github.com/getsops/sops/releases
# and https://github.com/FiloSottile/age/releases
```

### 3. Configure secrets (SOPS + Age)

All secrets (OVH API credentials and ayb configuration) are stored in a single SOPS-encrypted `secrets.yaml` file.

#### One-time SOPS/Age setup:

```bash
# Generate an age key (your private encryption key)
mkdir -p ~/.config/sops/age
age-keygen -o ~/.config/sops/age/keys.txt

# Get your public key (you'll need this in the next step)
grep 'public key:' ~/.config/sops/age/keys.txt
# Output: public key: age1xxxxxxxxxxxxxxxxx...

# Set the Age key file location (add to ~/.bashrc or ~/.zshrc for persistence)
export SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt

# Create SOPS config from template and update with your public key
cp .sops.yaml.example .sops.yaml

# Edit .sops.yaml and replace YOUR_AGE_PUBLIC_KEY_HERE with your age1xxx... key
```

#### Create and encrypt your secrets:

```bash
# 1. Create secrets file from template
cp secrets.yaml.example secrets.yaml

# 2. Fill in your actual secrets in secrets.yaml
#    - OVH API credentials: https://us.ovhcloud.com/auth/api/createToken
#    - ayb fernet key: ayb default_server_config | grep fernet_key
#    - SMTP credentials from your email provider

# 3. Encrypt it with SOPS
sops -e -i secrets.yaml
# The file is now encrypted in-place

# 4. Generate .kamal/secrets for Kamal deployment (see "Generate Kamal secrets" section below)
./.kamal/generate_secrets

# 5. To edit later (SOPS will decrypt in your editor, re-encrypt on save):
sops secrets.yaml
# After editing, regenerate .kamal/secrets:
./.kamal/generate_secrets

# 6. To view without editing:
sops -d secrets.yaml

## Usage

### 1. First-time SSH key setup

Before creating any VPS instances, generate and upload an SSH key (one-time setup):

```bash
# Create SSH key without passphrase (default)
uv run python ovh_tools.py ssh-key create --key-name ayb-deploy

# Or with a passphrase for extra security (prevents passphrase from entering shell history)
set +o history
uv run python ovh_tools.py ssh-key create --key-name ayb-deploy --passphrase "your-passphrase"
set -o history

# Force recreate existing SSH key (overwrites local key and re-uploads to OVH)
uv run python ovh_tools.py ssh-key create --key-name ayb-deploy --force
```

This will:
1. Generate an ED25519 SSH key pair at `~/.ssh/ayb-deploy` (if not exists, or overwrite with `--force`)
2. Upload the public key to your OVH account (or replace existing with `--force`)
3. Allow passwordless SSH access to all VPS instances

### 2. Installing a VPS

After purchasing a VPS through OVH's web interface, use this command to install it with your SSH key and add it to your inventory:

```bash
uv run python ovh_tools.py install \
  --service-name vps-xxxxx.vps.ovh.net \
  --os "Ubuntu 24.04"
```

**Note:** This command destructively rebuilds the VPS, erasing all existing data. You can run it multiple times to reinstall the VPS with a fresh OS.

This will:
1. Automatically find the matching OS image from OVH's available images
2. Install the VPS with the specified OS
3. Configure SSH key access (no password needed)
4. Fetch VPS details (IP, zone, memory, etc.)
5. Update `vps_instances.json` inventory (adds if new, updates if exists)

## Deploying with Kamal

[Kamal](https://kamal-deploy.org/) is a deployment tool that uses Docker for zero-downtime deploys.

### 1. Install Kamal

```bash
gem install kamal
```

### 2. (Optional) Verify server configuration

The `config/deploy.yml` file uses ERB templating to automatically read server IPs from `vps_instances.json`. No manual configuration needed!

### 3. Generate Kamal secrets

After creating/updating `secrets.yaml`, generate the `.kamal/secrets` file:

```bash
# Generate .kamal/secrets from secrets.yaml
./.kamal/generate_secrets
```

This automatically creates `.kamal/secrets` with all secrets from `secrets.yaml`. The generated file uses command substitution to decrypt values on-demand with SOPS.

**Important:** Run `./.kamal/generate_secrets` whenever you add/remove secrets in `secrets.yaml` to keep `.kamal/secrets` in sync.

### 4. Verify SSH access

Make sure you can SSH to your servers:

```bash
ssh -i ~/.ssh/ayb-deploy ubuntu@<server-hostname>
```

### 5. Configure Docker permissions

If your VPS already has Docker installed (common with Ubuntu images), the user may not have permissions to use Docker. Configure it using Kamal:

```bash
# Add ubuntu user to docker group on all servers
kamal server exec "sudo usermod -aG docker ubuntu"
```

This is a known Kamal limitation - `kamal server bootstrap` doesn't automatically configure user permissions when Docker is already installed.

### 6. Deploy

`kamal setup` is NOT idempotent and will fail if run on servers that already have containers running.

```bash
# FIRST TIME ONLY: Initial deployment to all servers
kamal setup

# All subsequent updates (updates all servers)
kamal deploy

# Check deployment status
kamal app containers

# View application logs
kamal app logs --follow

# Run ayb client commands in container
kamal app exec --interactive --reuse "ayb client --help"
```

### Adding More Servers

When you need to scale up and add more VPS instances:

```bash
# 1. Purchase new VPS through OVH web interface

# 2. Install with SSH key and add to inventory
uv run python ovh_tools.py install --service-name vps-xxxxx.vps.ovh.net --os "Ubuntu 24.04"

# 3. Configure Docker permissions on the new server
kamal server exec "sudo usermod -aG docker ubuntu" --hosts <new-server-ip>

# 4. Deploy to ALL servers (Kamal auto-detects new server via ERB template)
# Don't use kamal setup - it will fail on existing servers
kamal deploy
```
### Common Kamal Commands

```bash
kamal setup          # Initial setup (install Docker, deploy)
kamal deploy         # Deploy/update app (use for updates AND new servers)
kamal redeploy       # Force a fresh deploy (rebuilds images)
kamal rollback       # Rollback to previous version
kamal app logs       # View application logs
kamal app logs -f    # Follow logs in real-time
kamal app exec       # Execute commands in container
kamal app containers # List running containers
kamal app stop       # Stop the application
kamal app start      # Start the application
kamal accessory boot # Start accessories (if configured)
```

## Files

### VPS Management
- `ovh_tools.py` - OVH VPS management script with modes:
  - `ssh-key create` - Generate and upload SSH key (one-time setup)
  - `install` - Install VPS with SSH key and update inventory (auto-matches OS names to image IDs, can be called multiple times)
  - `add-to-inventory` - Add VPS to inventory without installing/rebuilding
- `vps_instances.json` - VPS instance inventory (auto-generated/updated by install and add-to-inventory)

### Secrets Management
- `secrets.yaml` - SOPS-encrypted secrets for both OVH and ayb (gitignored)
- `secrets.yaml.example` - Template showing structure of secrets file (committed to git)
- `.sops.yaml` - SOPS configuration with your Age public key (gitignored, created from template)
- `.sops.yaml.example` - Template for SOPS configuration (committed to git)
- `.kamal/secrets` - Auto-generated dotenv file with command substitutions to decrypt secrets (gitignored, regenerated via `.kamal/generate_secrets`)
- `.kamal/generate_secrets` - Script to generate `.kamal/secrets` from `secrets.yaml` structure (committed to git)

### Deployment
- `config/deploy.yml` - Kamal deployment configuration (uses ERB templating)