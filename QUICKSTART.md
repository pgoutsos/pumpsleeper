# PumpSleeper — Quick Start

Flash the image, boot your Pi, and set up the dashboard in about 15 minutes.

## What you'll need

- A Raspberry Pi (Pi 4 recommended for reliability; Pi Zero 2 W also supported)
- A microSD card (8 GB+) and a card reader
- A solid power supply for the Pi
- An internet connection for the Pi's first boot — **ethernet is easiest**; home Wi-Fi works too
- Your PumpSpy device

---

## 1. Flash the image

1. Download the latest `pumpsleeper-vX.X.img.xz` from the [Releases page](https://github.com/pgoutsos/pumpsleeper/releases).
2. Install and open **[Raspberry Pi Imager](https://www.raspberrypi.com/software/)**.
3. Click **Choose OS → Use custom**, and select the `.img.xz` you downloaded.
4. Click **Choose Storage** and pick your SD card.
5. Click **Write** and wait for it to finish.

> If Imager offers an "OS customization" prompt, you can skip it — PumpSleeper handles its own setup.

---

## 2. Configure before first boot

After flashing, the SD card shows up as a drive named **bootfs** (or **boot**). Open it on your computer and edit **`pumpsleeper.conf`** with any plain text editor.

The hotspot your PumpSpy device joins already has defaults filled in — you can leave them as-is or change them:

```
HOTSPOT_SSID=PumpSpyLab      (default — change if you like)
HOTSPOT_PASS=pumpspy123      (default — change if you like)
WIFI_COUNTRY=US
```

If you change either, just remember the values — you'll enter the same SSID and password on your PumpSpy device in step 6.

**Only if you're not using ethernet**, add your home Wi-Fi so the Pi can reach the internet to install. If you're plugged into ethernet, leave these blank:

```
HOME_WIFI_SSID=your-home-wifi
HOME_WIFI_PASS=your-home-wifi-password
```

Optional:

- `SSH_PASS` — the Pi's login password. Default is `pumpspy`; change it here if you want.
- `MQTT_HOST` — leave blank unless you want Home Assistant integration; set it to your MQTT broker's address to enable it.

Save the file and safely eject the card.

---

## 3. First boot

1. Put the SD card in the Pi, connect ethernet (if using it), and power it on.
2. The first boot installs everything automatically — **give it 3–5 minutes**. Don't unplug it.
3. To watch progress, you can re-insert the card in your computer and open **`pumpsleeper-install.log`** on the boot drive. The last line will read **`Installation complete!`** when it's done.

---

## 4. Open the dashboard

In a browser on the same network as the Pi, go to:

```
http://pumpsleeper.local:8080
```

If that name doesn't resolve, use the Pi's IP address instead: `http://<pi-ip>:8080`.

**Log in with the factory credentials:**

- Username: `admin`
- Password: `admin`

The **Dashboard** shows today's pump activity, total gallons, device health (signal, backup battery), the PumpSpy device status, and pump run history.

![Dashboard](images/dashboard.png)

---

## 5. Set up Settings

Open the **Settings** tab. Work top to bottom.

![Settings](images/settings.png)

**Appearance** — pick a theme (Auto / Dark / Light). Auto follows your device's light/dark setting.

**Security & Web Access (do this first)**

1. Edit the **User Name** field and click **Save User Name**.
2. Click **Change Password** and set a real password.
   - You must change the default `admin` / `admin` before web access can be enabled.
3. (Optional) **Web Access** lets you reach the dashboard from outside your home over a Cloudflare tunnel:
   - **Quick tunnel** — no account needed; the address changes each restart.
   - **My Cloudflare tunnel** — a stable address on your own domain (paste your tunnel token + public hostname, then **Save tunnel settings**).
   - Tick **Enable web access**. Your public link appears under **Public URL**.

**Email & ntfy Notifications**

- **Email** — tick *Enable email notifications*, fill in your SMTP host/port, username, app password, from/to addresses, then **Send test email**.
- **ntfy** — tick *Enable ntfy notifications*, set the server URL and a private topic name, then **Send test notification**.
- Notification settings save automatically; alerts include a tap-through link back to the dashboard.

**Notification Triggers** — tick which events alert you: Backup pump ran, Main pump ran, High water alert, Device offline, New version available, New version installed.

**Updates** — shows your current vs. latest version. Leave *Automatically install updates overnight* on, or use **Check now**.

**Backup & Restore**

- **Download backup** saves your settings (notifications, login, web access, theme) to a file. ⚠️ It includes your saved credentials — keep it somewhere safe.
- **Restore from backup…** loads those settings onto a new Pi after reimaging.
- Optionally tick **Email me a backup automatically (weekly)**.

---

## 6. Connect your PumpSpy device

Join your PumpSpy device to the hotspot you set in step 2 (`HOTSPOT_SSID` / `HOTSPOT_PASS`). Once it connects, the dashboard's device card will start showing live data and pump history.

---

## Troubleshooting

- **Dashboard won't load** — give the first boot the full 3–5 minutes; then try the Pi's IP instead of `pumpsleeper.local`.
- **Install seems stuck or the Pi keeps rebooting** — check `pumpsleeper-install.log` on the boot drive for the last step it reached. (This is rare on v3.7+.)
- **PumpSpy device won't join the hotspot** — double-check the password matches `HOTSPOT_PASS` exactly.
- **SSH in** — `ssh pumpsleeper@pumpsleeper.local` (password `pumpspy`, or whatever you set as `SSH_PASS`).

More info: https://github.com/pgoutsos/pumpsleeper
