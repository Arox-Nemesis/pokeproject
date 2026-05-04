# Runtime Restart Implementation Report

Implementing a runtime restart feature for a bot running inside a Docker container presents an interesting architectural challenge. Because the bot lives *inside* the container, asking it to rebuild and replace its own container is like asking someone to rebuild the house they are currently standing inside.

Here is a detailed breakdown of how to implement this securely and effectively, satisfying both your "Plain Restart" and "Rebuild Restart" requirements.

## 1. Mode 1: Plain Restart
This mode simply restarts the bot process. It clears out all in-memory states and starts fresh. 

**How it works:**
Since your `docker-compose.yml` uses the `restart: unless-stopped` policy, Docker acts as a process manager. If the Python script exits, Docker will immediately spin up a new container to replace it.

**Implementation:**
```python
import sys
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from telemon.config import BOT_OWNER_ID

router = Router()

@router.message(Command("restart"))
async def cmd_restart(message: Message) -> None:
    if message.from_user.id != BOT_OWNER_ID:
        return
        
    await message.answer("🔄 Restarting bot... I will be back online in a few seconds.")
    
    # Gracefully exit the process. Docker will automatically restart the container.
    sys.exit(0)
```
*Note: For your `telemon_premium_bot` which has `./src:/app/src` mapped as a volume, a plain restart will automatically pick up any new code changes made on the host without needing a rebuild!*

---

## 2. Mode 2: Rebuild & Restart
Your `telemon_bot` does not map the code as a volume; instead, the code is copied into the image during `docker build`. To use the latest code, the Docker image must be rebuilt. 

Your `Dockerfile` is already perfectly optimized for this! Because `pip install` happens *before* `COPY src/ ./src/`, Docker caches the dependencies. A rebuild will skip reinstalling packages and take less than 2 seconds to just copy the new code.

To allow the bot to trigger this rebuild from the inside, you have three options.

### Option A: The "Watchdog" Approach (Recommended for Security)
Instead of giving the container dangerous privileges, the bot sets a flag in Redis, and a safe script on your host server executes the rebuild.

1. **Bot Command:** The bot sets a Redis key `REBUILD_REQUESTED = True` and sends a "Rebuilding..." message.
2. **Host Script:** You run a small background bash script on your VPS/host:
```bash
#!/bin/bash
while true; do
    # Check if bot requested a rebuild via Redis
    if docker exec telemon_redis redis-cli get REBUILD_REQUESTED | grep -q "True"; then
        echo "Rebuild requested by bot owner!"
        docker exec telemon_redis redis-cli del REBUILD_REQUESTED
        
        # Optional: git pull
        # git pull origin main
        
        # Rebuild and restart only the python containers
        docker-compose up -d --build bot bot_premium
    fi
    sleep 5
done
```

### Option B: The "Docker Socket" Approach (Most Direct)
If you want the bot to issue the Docker commands directly, you must map the host's Docker socket into the container. This gives the container the ability to control the host's Docker daemon.

**1. Update `docker-compose.yml`:**
```yaml
  bot:
    build: .
    container_name: telemon_bot
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock  # Grants docker access
```

**2. Update `Dockerfile`:**
Install the Docker CLI so the bot can run commands.
```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends docker.io docker-compose-v2
```

**3. Implementation:**
```python
import subprocess

@router.message(Command("rebuild"))
async def cmd_rebuild(message: Message) -> None:
    if message.from_user.id != BOT_OWNER_ID:
        return
        
    await message.answer("🏗️ Rebuilding Docker container with latest code...")
    
    # Asynchronously trigger the rebuild. 
    # The current container will be killed abruptly when Docker replaces it.
    subprocess.Popen([
        "docker", "compose", "up", "-d", "--build", "bot"
    ])
```

### Option C: The "Volume Mount" Approach (The No-Rebuild Hack)
If you add `- ./src:/app/src` to the `bot` service in `docker-compose.yml` (just like you did for `bot_premium`), the container will always use the live code from the host. 
In this setup, a true "rebuild" is never needed for code changes. You just do a `git pull` on the host, and trigger Mode 1 (Plain Restart) via the bot.

## Summary
To implement this today without completely changing your security posture, **Mode 1** is trivial to add (`sys.exit(0)`). For **Mode 2**, I recommend **Option A (Watchdog)** or **Option C (Volume Mapping)** to avoid exposing the host's Docker socket to the Python container.
