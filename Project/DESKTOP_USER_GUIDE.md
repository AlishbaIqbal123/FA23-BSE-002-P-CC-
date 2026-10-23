# Distributed Computing System — Desktop User Guide

A beginner-friendly guide to running the Distributed Task Offloading & GPU Remote Rendering System across two desktop/laptop computers connected over a Local Area Network (LAN) **without using Shell or Command Prompt**.

---

## 🎥 Visual Walkthrough of the Live Demo Videos

This guide directly documents the exact workflow demonstrated in the two bundled screen recordings:
* **[Client-side Clipchamp & Remote Offload](screenrecordigs/Client-side-edited-and-render-video-of-clip-champ-remote.mp4)** (`screenrecordigs/Client-side-edited-and-render-video-of-clip-champ-remote.mp4`)
* **[Server-side Daemon & Execution Node](screenrecordigs/server%20side.mp4)** (`screenrecordigs/server side.mp4`)

---

## 🚀 Quick Start in 3 Steps (Zero Shell / No CMD Required!)

### Step 1: On the Server Computer (The machine with the GPU or heavy compute power)
1. Double-click **`Launch_Server.vbs`** (or use the Desktop shortcut **`Distributed System - Server Node`**).
2. The **Server Node Control Center** opens.
3. Your **LAN IP address** is displayed in bold in the blue box (e.g. `192.168.100.3`). Click **"📋 Copy IP"**.
4. Click the button: **`▶ Start Worker Server`**.
   - The status pill will turn **`🟢 LISTENING :7575`**.
   - If Windows Firewall prompts you, click **Allow Access**, or click the **"🛡️ Firewall Rule"** button in the app.

---

### Step 2: On the Client Computer (Laptop or Desktop offloading tasks)
1. Double-click **`Launch_Client.vbs`** (or use the Desktop shortcut **`Distributed System - Client Controller`**).
2. In the **Worker Connection** box on the left:
   - Click **`🔍 Scan`**. The app automatically scans your LAN and finds your Server!
   - Select the discovered server from the dropdown list.
   - *(Alternatively, paste the Server IP you copied in Step 1 into the "Worker IP" box, e.g. `192.168.100.3`)*.
3. Click **`Connect`**.
   - The status pill turns **`🟢 ONLINE`**.
   - The server's GPU, NVENC, CUDA, and CPU capabilities are automatically negotiated and displayed.
   - Latency (RTT) and jitter are measured in real time (e.g. **26.62 ms** as seen in the demo).

---

### Step 3: Offload a Task!

#### 🎬 Automated Clipchamp / Video Editor Watch Folder (As demonstrated in Video 1):
1. Switch to the **`🎬 Clipchamp Watch Folder`** tab in the Client App.
2. The client actively monitors your **Downloads** folder (`C:\Users\<user>\Downloads`) where Microsoft Clipchamp, CapCut, or Premiere exports files.
3. Click **`🎬 Launch Clipchamp`** to edit your video project.
4. When you click **Export** in Clipchamp:
   - The watcher detects when the export write finishes and file locks release (`Purple canyon YouTube intro.mp4 (19.69 MiB)`).
   - If **"⚡ Auto-offload to GPU immediately upon export"** is checked, it automatically transfers the video over LAN and renders it on the server node without requiring you to click a button!
   - Live dual progress bars track both network transfer (19.6 Mb/s) and remote worker execution (0% ➔ 100%).
   - Once completed, the master video downloads back (21.9 Mb/s) and undergoes SHA-256 integrity verification.
   - **Size Savings**: The original **19.69 MiB** draft is compressed to **2.67 MiB** (**86.4% reduction**) while keeping full 1080p resolution!
   - Click **"Open Output Folder"** to view and play your rendered MP4 file.

#### Manual Video Transcoding:
1. Click **`📂 Browse File...`** (or drag & drop any video file into the window).
2. Select your desired profile (e.g., **1080p Full HD**, **NVENC Hardware Accelerated**, or **H.264**).
3. Click **`▶ Submit Job`**.

#### Tensor Compute:
1. Switch to the **`Tensor Compute`** tab.
2. Select an operation (**Matrix Multiplication**, **2D Convolution**, or **Transformer Feed-Forward**).
3. Click **`▶ Submit Job`**. The server computes on CUDA / NumPy and returns exact benchmark timing and FLOPS metrics.

---

## 🌟 The Unified Desktop Control Hub (`desktop_hub.py`)

If you are not sure whether a computer should be a server or a client, double-click **`Launch_Hub.vbs`** (or **`Launch_Hub.bat`**):

- Presents a unified dashboard with two large mode cards:
  - **🖥️ Server Node (Worker)** — Launch the compute server.
  - **💻 Client Controller (Task Offloader)** — Launch the task offloading GUI.
- Built-in **Desktop Utilities**:
  - **`🔍 LAN Diagnostics`**: View all local network IP addresses and test reachability to any computer.
  - **`🛡️ Windows Firewall Helper`**: Configure Windows Firewall with one click so both computers can communicate without being blocked.
  - **`📌 Create Desktop Shortcuts`**: Adds 1-click shortcuts directly to your Windows Desktop!

---

## 🛠️ Network Setup & LAN Connection Guide

Both computers must be on the same local network. This can be:
1. **The same Wi-Fi router / network** (e.g. `192.168.100.x` as used in the demo).
2. **The same Ethernet switch / router**.
3. **A direct Ethernet cable** plugged between both computers' network ports.
4. **A mobile hotspot** created on a phone or laptop.

### Auto-Discovery (UDP Port 7576)
- The server broadcasts a zero-configuration beacon on UDP 7576.
- The client scanner detects this beacon in under 1 second, meaning you don't even have to look up IP addresses!

### Troubleshooting Connection Issues:
| Problem | Cause | Solution |
|---|---|---|
| Client says *Connection timed out* | Windows Firewall is blocking incoming traffic on the server, or university Wi-Fi has AP isolation. | On the server app, click **"🛡️ Firewall Rule"**, or connect both PCs to a phone mobile hotspot. |
| Client says *Connection refused* | The server is not started yet. | On the server app, ensure the button shows **"⏹ Stop Worker Server"** and status is **🟢 LISTENING :7575**. |
| Virtual adapter IP selected | VMware or WSL virtual network picked by mistake. | The system now automatically prioritizes physical Wi-Fi/Ethernet adapters and marks virtual adapters with `(Virtual)`. |
| Direct cable without router | Windows uses APIPA link-local (`169.254.x.x`). | The system supports link-local and all LAN ranges automatically. Just use the **LAN Diagnostics** tool or click **Scan**. |

---

## 📁 File & Output Locations

- **Client Output**: Transcoded videos and compute benchmark results are automatically downloaded and verified in:
  `client/data/outputs/`
- **Server Cache**: Staged job files and temporary rendering buffers reside in:
  `server/data/jobs/<job_id>/`
- You can click **"Open Storage Folder"** or **"Open Output Folder"** in either GUI to open the Windows File Explorer folder directly.
