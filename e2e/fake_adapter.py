"""假装是 xlt-channel：只看信封，把回信请求写进 outbox。"""

import argparse
import json
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--window", required=True)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    home = Path(args.home)
    inbox = home / "inbox" / args.window
    outbox = home / "outbox" / args.window
    alive = home / "channels" / f"{args.window}.alive"
    log_path = Path(args.log)
    inbox.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    alive.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    while True:
        alive.write_text(str(time.time()), encoding="utf-8")
        for env_path in sorted(inbox.glob("*.env.json")):
            try:
                env = json.loads(env_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            status = (env.get("state") or {}).get("status")
            msg_id = env.get("id")
            if status != "queued" or not msg_id or msg_id in seen:
                continue
            seen.add(msg_id)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "id": msg_id,
                            "from": env.get("from"),
                            "type": env.get("type"),
                            "subject": env.get("subject"),
                            "bytes": env.get("letter_bytes"),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            if env.get("subject") == "出口检查":
                text = "-----BEGIN PRIVATE KEY-----\nMIIEfake\n-----END PRIVATE KEY-----"
            else:
                text = "收到 " + msg_id
            env.setdefault("state", {})["status"] = "delivered"
            env["state"]["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            tmp = env_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(env, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(env_path)
            reply = outbox / f"{int(time.time() * 1000)}-{msg_id}.json"
            payload = {"op": "xlt_reply", "msg_id": msg_id, "text": text, "status": "replied"}
            raw = reply.with_suffix(".json.tmp")
            raw.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            raw.replace(reply)
        time.sleep(0.2)


if __name__ == "__main__":
    main()
