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
- A first-admin setup token printed in the service logs
- Automatic migrations at service startup

Render handles the always-on web URL, TLS certificate, service restart, database
hosting, and platform-level database backups. Lab admins still control user
roles and project membership inside Lab Tracker.

## First Admin

1. Click **Deploy to Render**.
2. Connect or fork the GitHub repo when Render asks.
3. Wait for the first deploy to finish.
4. Open the Lab Tracker service logs and copy `First admin setup token`.
5. Open the service URL, choose `Create First Admin`, and paste the token.

After the first admin exists, use `Users` to invite lab members by email, grant
viewer/editor/admin roles, and reset passwords. Use each project's
`Project Members` panel for project viewer/contributor/owner access.

## Invitation Links

The `Users` screen creates signed invitation links. If
`LAB_TRACKER_PUBLIC_BASE_URL` is set, links use that URL. Otherwise links use
the host from the browser request. On Render, the Docker entrypoint also uses
`RENDER_EXTERNAL_URL` when available.

Invitation links expire after `LAB_TRACKER_AUTH_INVITE_TTL_HOURS` hours
(default: 168). The invited member opens the emailed link, sets a password, and
is signed in with the role encoded in the invitation.

## Operational Notes

- Keep `LAB_TRACKER_AUTH_ENABLED=true` for cloud deployments.
- Keep uploaded files and runtime secrets on the persistent disk mounted at
  `/var/data`.
- If Render shows a different public URL after deploy, set
  `LAB_TRACKER_PUBLIC_BASE_URL` to that URL so future email invitations use the
  stable address.
- Upgrade by redeploying the latest repo revision. The container applies
  migrations before serving traffic.
- Use Render's database backup and restore tools for the managed Postgres
  database. For manual self-hosted Docker backup commands, see
  [`self-hosted-operations.md`](self-hosted-operations.md).
