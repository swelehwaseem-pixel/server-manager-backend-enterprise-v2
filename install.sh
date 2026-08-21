#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ "${EUID}" -eq 0 ]]; then
  echo "Please run this installer as a regular user with sudo access."
  exit 1
fi

if [[ ! -f /etc/os-release ]]; then
  echo "Cannot detect operating system."
  exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release

case "${ID}" in
  ubuntu|debian)
    sudo apt-get update
    sudo apt-get install -y ca-certificates curl git openssl
    ;;
  rhel|rocky|almalinux|centos|fedora|ol)
    sudo dnf install -y ca-certificates curl git openssl dnf-plugins-core
    ;;
  *)
    echo "Unsupported OS: ${ID}"
    exit 1
    ;;
esac

install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    echo "Docker and Compose are already installed."
    return
  fi

  echo "Installing Docker Engine and Compose plugin..."
  case "${ID}" in
    ubuntu|debian)
      sudo install -m 0755 -d /etc/apt/keyrings
      curl -fsSL "https://download.docker.com/linux/${ID}/gpg" | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
      sudo chmod a+r /etc/apt/keyrings/docker.gpg
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
      sudo apt-get update
      sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      ;;
    rhel|rocky|almalinux|centos|ol|fedora)
      sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
      sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      sudo systemctl enable --now docker
      ;;
  esac
}

install_docker
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER" || true

mkdir -p prometheus_targets
sudo mkdir -p /backup/oracle /backup/mssql /etc/letsencrypt
sudo chmod 755 prometheus_targets /backup/oracle /backup/mssql

if [[ ! -f .env ]]; then
  echo "Creating .env from .env.example..."
  cp .env.example .env
  sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$(openssl rand -hex 32)/" .env
  sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
  sed -i "s/^GRAFANA_PASSWORD=.*/GRAFANA_PASSWORD=$(openssl rand -hex 16)/" .env
  sed -i "s/^FIRST_SUPERUSER_PASSWORD=.*/FIRST_SUPERUSER_PASSWORD=$(openssl rand -hex 16)/" .env
fi

# Select the RHEL-specific image automatically on RHEL-compatible hosts.
if [[ "${ID}" == "rhel" || "${ID}" == "rocky" || "${ID}" == "almalinux" || "${ID}" == "centos" || "${ID}" == "ol" || "${ID}" == "fedora" ]]; then
  if ! grep -q '^SERVER_MANAGER_DOCKERFILE=' .env; then
    printf '\nSERVER_MANAGER_DOCKERFILE=Dockerfile.rhel\n' >> .env
  fi
fi

# Validate the compose application before starting it.
docker compose config >/dev/null

echo "Building and starting Server Manager..."
docker compose up -d --build

echo "Waiting for readiness..."
for _ in {1..60}; do
  if curl -fsS http://127.0.0.1/health/ready >/dev/null 2>&1; then
    echo "Server Manager is ready."
    break
  fi
  sleep 2
done

if ! curl -fsS http://127.0.0.1/health/ready >/dev/null 2>&1; then
  echo "Server Manager did not become ready. Showing backend logs:"
  docker compose logs --tail=100 server-manager-backend || true
  exit 1
fi

sudo cp deployment/server-manager.service /etc/systemd/system/server-manager.service
sudo systemctl daemon-reload
sudo systemctl enable server-manager.service

cat <<EOF

Server Manager installation completed.

API:       http://127.0.0.1/ 
Health:    http://127.0.0.1/health
Readiness: http://127.0.0.1/health/ready
Grafana:   http://127.0.0.1:${GRAFANA_PORT:-3000}
Prometheus: http://127.0.0.1:${PROMETHEUS_PORT:-9090}

The generated credentials are stored in .env.
To manage the stack:
  docker compose ps
  docker compose logs -f server-manager-backend
  docker compose down
  docker compose up -d

To enable automatic startup after reboot:
  sudo systemctl enable --now server-manager.service
EOF
