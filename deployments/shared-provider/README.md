# Pinned shared-provider deployment overlay

This overlay keeps an authenticated, provider-backed Lab Tracker instance on
one reviewed immutable application image. It extends the repository-root
`docker-compose.yml`, removes both source-build definitions, and applies the
same pinned image to the app and MCP services.

Put only the provider credential in the ignored file
`deployments/shared-provider/.env`:

```dotenv
LAB_TRACKER_OPENAI_API_KEY=replace-with-provider-key
```

Copy `runtime.env.example` to the ignored `runtime.env` beside it and set the
non-secret provider, scheduler, model, timeout, and review-email policy there.
Keeping this policy in a service-level environment file prevents production
settings from leaking into host-side tests and CLI commands.

In the ignored repository-root `.env`, select the overlay and configure only
the immutable image and ordinary root-Compose settings:

```dotenv
COMPOSE_FILE=docker-compose.yml:deployments/shared-provider/docker-compose.yml
LAB_TRACKER_RELEASE_IMAGE=lab-tracker-primary:sha-<full-git-revision>
```

The `COMPOSE_FILE` separator shown above is for macOS/Linux. Use `;` on
Windows, or set `COMPOSE_PATH_SEPARATOR` explicitly.

Before changing the live service, verify that ordinary Compose resolution has
no build definition and gives app and MCP the same immutable image:

```bash
docker compose config --format json | jq \
  '{app: .services.app | {image,build}, mcp: .services.mcp | {image,build}}'
```

Both `build` values must be `null`. If the overlay, provider file, runtime file,
or release image is missing, Compose fails before replacing a container. Use
ordinary commands such as `docker compose up -d app mcp` only after that check
passes.
