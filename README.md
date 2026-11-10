# SatelliteCall Termux + ngrok

Host the SatelliteCall Internet-calling backend on an Android phone. A Python launcher checks the local server, starts or reuses an ngrok tunnel, and prints the HTTPS address to enter in the SatelliteCall app.

## Requirements

- Android with [Termux](https://f-droid.org/en/packages/com.termux/).
- Python in Termux. Install it with `pkg install python` if needed.
- The ngrok command available in Termux and [configured with your ngrok account](https://ngrok.com/docs/gateway/endpoints/agent-cli-quickstart).
- SatelliteCall installed on both phones.

## Start the server

Extract the project into a folder that Termux can read. If you extracted it in Downloads, run `termux-setup-storage` once and allow access when prompted. In Termux, open the extracted project folder and run:

```sh
python start.py
```

The launcher checks port `8080`, reuses a healthy SatelliteCall server if available, and starts the bundled server if needed. It also reuses an ngrok tunnel that already forwards to the correct port. If it reports an older ngrok tunnel pointing to another port, stop that tunnel in its Termux session with **Ctrl+C** and run the launcher again.

## Connect the app

1. Wait for **Public server check pass** and copy the HTTPS address printed on the last line.
2. Enter that address on both phones in SatelliteCall’s **Internet / satellite IP setup**. Keep `/healthz` for browser checks.
3. Create or use a device account, then share the contact code shown in Internet mode to place a call. Keep the host’s server and ngrok sessions running.

The tunnel forwards call signalling. WebRTC carries the audio directly when the two phones can connect. If a direct audio connection fails, the backend can provide TURN credentials through the `SATCALL_TURN_URLS` and `SATCALL_TURN_SECRET` environment variables.

## Database and logs

The launcher reuses a known local SQLite database with existing accounts when available. It prints the selected path when starting the server. To choose a specific database, set `SATCALL_DB` before running `start.py`. If there is no existing database, the default is `~/.satellitecall-ngrok/satellitecall.sqlite3`.

```sh
tail -n 20 ~/.satellitecall-ngrok/server.log
tail -n 20 ~/.satellitecall-ngrok/ngrok.log
```

Press **Ctrl+C** to stop the processes the launcher started. Any server or ngrok process started in another session stays in that session.

## Project files

- `start.py` starts or reuses the server and tunnel, then checks and displays the app URL.
- `server.py` contains the SatelliteCall signalling API and account storage.
- `.gitignore` excludes local databases, logs, ngrok configuration files and private keys from Git tracking.

Store your ngrok authtoken in the local agent configuration on the hosting phone.
