"""端到端用的假 headless。读 stdin，不把正文写回；只回复消息 id。"""

import os
import sys

sys.stdin.read()
msg_id = os.environ.get("XLT_MSG_ID", "")
sys.stdout.write(f"已处理 {msg_id}")
