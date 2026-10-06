# One-Click Cloud Deploy

Lab Tracker includes a Render Blueprint (`render.yaml`) for labs that want a
managed deployment without running terminal commands on a lab computer.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/SamuelBrudner/lab-tracker)

## What The Blueprint Creates

- A Docker-backed Lab Tracker web service
- A managed Postgres database
- A persistent disk for uploaded files, note storage, and generated runtime
  secrets
- A generated auth signing secret
- A generated first-admin setup token, readable in your Render dashboard and
  never shown by the app itself
- Automatic migrations at service startup

Render handles the always-on web URL, TLS certificate, service restart, database
hosting, and platform-level database backups. Lab admins still control user
roles and project membership inside Lab Tracker.

## First Admin

1. Click **Deploy to Render**.
2. Connect or fork the GitHub repo when Render asks.
3. Wait for the first deploy to finish.
4. In the Render dashboard, open the `lab-tracker` service, go to
   **Environment**, and copy the value of `LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN`.
5. Open the service URL, choose `Create First Admin`, paste the token, and
   choose a username and password to create the admin account.

The Render Blueprint sets `LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN_DISCLOSURE=never`,
so the app never shows the token in the browser. The service URL is public as
soon as the deploy finishes, and `first_run` would hand the token, and with it
the admin account, to anyone who opened the URL before you did. The token stops
working once the first user exists.

After the first admin exists, use `Users` to invite lab members by email, grant
viewer/editor/admin roles, and reset passwords. Use each project's
`Project Members` panel for project viewer/contributor/owner access.

## Invitation Links

The `Users` screen creates single-use invitation links. Pending invitations are
listed on the same screen and can be revoked before they are consumed. If
`LAB_TRACKER_BASE_URL` is set, links use that origin. Otherwise links use
the host from the browser request and warn when the host is local or private.
On Render, the Docker entrypoint also uses `RENDER_EXTERNAL_URL` when available.

Invitation links expire after `LAB_TRACKER_AUTH_INVITE_TTL_HOURS` hours
(default: 168). The invited member opens the emailed link, sets a password, and
is signed in with the role encoded in the invitation.

## Operational Notes

- Keep `LAB_TRACKER_AUTH_ENABLED=true` for cloud deployments.
- Keep uploaded files and runtime secrets on the persistent disk mounted at
  `/var/data`.
- If Render shows a different public URL after deploy, set
  `LAB_TRACKER_BASE_URL` to that origin so future email invitations use the
  stable address.
- Upgrade by redeploying the latest repo revision. The container applies
  migrations before serving traffic.
- Use Render's database backup and restore tools for the managed Postgres
  database. For manual self-hosted Docker backup commands, see
  [`self-hosted-operations.md`](self-hosted-operations.md).
