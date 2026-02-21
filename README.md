# VLC Sync

**VLC Sync** is a lightweight tool that lets you watch videos with friends in perfect synchronization.

It is inspired by **Syncplay** but built specifically to handle **unstable internet connections**. Unlike other tools, if you disconnect or lag, VLC Sync will automatically reconnect and seamlessly sync you back to the group without needing a manual restart.

---

## Requirements

### Server
- **Python 3.10+**

### Client
- **Python 3.10+**
- **VLC Media Player**

---

## Installation & Usage

### 1. Server Setup (The Host)

You need one computer to act as the server.

1. **Clone the repository:**
   ```bash
   git clone https://github.com/hammadbawara/vlcsync.git
   cd vlcsync
   ```

2. **Run the server:**
   Replace `9000` with any port you like.
   ```bash
   python3 server.py 9000
   ```

#### Run as a Background Service
To keep the server running automatically (even after reboot), use the included script:

```bash
# Usage: sudo ./scripts/add_server_systemd_service.sh <PORT>
sudo ./scripts/add_server_systemd_service.sh 9000
```

Then start it:
```bash
sudo systemctl start vlcsync-server
sudo systemctl enable vlcsync-server  # Optional: Start on boot
```

---

### 2. Client Setup (The Viewers)

Every person watching needs to run the client.

1. **Clone the repository:**
   ```bash
   git clone https://github.com/hammadbawara/vlcsync.git
   cd vlcsync
   ```

2. **Run the client:**
   You need a **Username**, the **Server IP**, and the **Port**.
   ```bash
   python3 client.py <Username> <Server_IP> <Port>
   ```

   **Example:**
   ```bash
   python3 client.py Alice 192.168.1.50 9000
   ```

   *Note: On the first run, it automatically installs a small Lua script into VLC.*
   Install location by platform:
   - Linux: `~/.local/share/vlc/lua/intf` (or Flatpak: `~/.var/app/org.videolan.VLC/data/vlc/lua/intf`)
   - macOS: `~/Library/Application Support/org.videolan.vlc/lua/intf`
   - Windows: `%APPDATA%\vlc\lua\intf`

   You can override the target directory by setting:
   ```bash
   export VLCSYNC_VLC_LUA_DIR="/custom/path/to/vlc/lua/intf"
   ```

3. **Watch Together:**
   VLC will open automatically. Open your video file, and when you Play/Pause/Seek, it will sync with everyone else!

---

## Configuration

After running the client once, a config file is created at:
`./config/client_config.ini`

You can potentiall save your details here so you don't have to type them every time:

```ini
[client]
username = Alice
server_ip = 1.2.3.4
port = 9000
lua_port =
```

Now you can just run:
```bash
python3 client.py
```