import re

with open("/root/wpss_project/wpss/mcp_server/server.py", "r") as f:
    lines = f.readlines()

new_lines = []
for i, line in enumerate(lines):
    if "full_response += chunk" in line and i in [578, 579, 580, 581, 582, 583]:
        new_lines.append("                    full_response += chunk\n")
    elif "if not full_response:" in line and i in [575, 576, 577, 578]:
        new_lines.append("                    if not full_response:\n")
    elif "logger.info(\"First chunk received from backend\")" in line and i in [576, 577, 578, 579]:
        new_lines.append("                        logger.info(\"First chunk received from backend\")\n")
    elif "if len(full_response) % 10000 < len(chunk):" in line and i in [577, 578, 579, 580]:
        new_lines.append("                    if len(full_response) % 10000 < len(chunk):\n")
    elif "logger.info(f\"Received {len(full_response)} bytes so far...\")" in line and i in [578, 579, 580, 581]:
        new_lines.append("                        logger.info(f\"Received {len(full_response)} bytes so far...\")\n")
    else:
        new_lines.append(line)

with open("/root/wpss_project/wpss/mcp_server/server.py", "w") as f:
    f.writelines(new_lines)
