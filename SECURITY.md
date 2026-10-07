# Security and data handling

Never submit real tokens, service-account JSON, DPAPI blobs, account IDs, course exports, private documents, runtime logs or Calendar bindings in public issues, pull requests or Actions artifacts. Report vulnerabilities through the repository's private vulnerability reporting feature when available; otherwise request a private contact channel without including secrets.

The application reads authorized LMS data. Professor-provided text and safe scheduling evidence are used by the user's Codex model for interpretation and independent QA. Local snapshots can contain personal academic information and downloaded documents. The configured ntfy destination receives learning summaries; Google receives managed event details. These destinations must be reviewed and authorized by each user. A topic name is not a strong access-control mechanism.

Windows Credential Manager stores the Canvas PAT and legacy Desktop OAuth credentials. Service-account keys use current-user DPAPI under LOCALAPPDATA. Only the configured Windows credential owner may launch normal workers. Permission checks and independent QA are not disabled by installation or scheduling. Never change global approval policy to make a failed run appear successful.

Private state is excluded by .gitignore and never included in a release. The release checker is a defense in depth, not proof that arbitrary new text is non-personal. Inspect staged changes and run a separate secret scanner before publishing. If a real credential was uploaded, revoke it; deleting a commit alone is insufficient.

Back up configuration, state, snapshots and bindings privately. DPAPI blobs are tied to the Windows user context and are not a portable cloud credential backup. Public Git history contains source and synthetic tests only.
