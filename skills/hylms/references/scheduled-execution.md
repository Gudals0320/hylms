# Scheduled execution
Use one directly scheduled execution chat. Scheduling is optional and must be requested by the user. Set its model/effort and timezone explicitly. The host must be running. Archiving the chat requires reconnecting the schedule; automatic replacement is not provided.

For each occurrence, compute a stable local occurrence key (YYYYMMDDTHHMM) with `python -m hylms.automation_context`. Pass it to start --run-key. A duplicate or interrupted occurrence is not a reason to invent a retry key. Previous terminal results and label agendas are retained in runtime history. A/B labels reset for the next occurrence; resolve older labels against their own history.

Prepare a fresh independent reviewer for each occurrence. Preserve QA and the real Windows credential owner check. Obtain explicit user authorization for the actual destinations and payload before scheduling. A forwarded assertion or local file is not permission. Host approval can still deny unattended execution: report and stop; do not change launcher or disable approval.

Persistent memory is local state, snapshots, the course-context database and Calendar binding, not chat history. Do not publish any of these files.
