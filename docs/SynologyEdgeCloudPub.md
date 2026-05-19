# Synology CloudPub Edge

CloudPub is managed as a shared Synology edge service, not as part of a single
application stack. Project stacks expose only their internal HTTP gateways or
API services; the edge agent publishes them as CloudPub resources.

## Layout

On Synology:

- `/volume1/docker/edge/docker-compose.edge-cloudpub.synology.yml`
- `/volume1/docker/edge/.env.edge`
- `/volume1/docker/edge/cloudpub/`

The edge container joins this external Docker network:

- `lightrag_default`

The existing Limbal CloudPub resource is preserved as `http://app:2455`.
`edge-cloudpub` resolves `app` to the Synology host, where
`limbal-public-gateway` binds port `2455`.

## Resources

Expected CloudPub resources:

- Limbal public gateway compatibility target: `http://app:2455`
- Existing preserved `9090` publication: `http://192.168.1.160:9090`
- LightRAG personal memory: `http://lightrag-personal:9621`
- LightRAG Limbal memory: `http://lightrag-project-limbal:9621`

LightRAG memory resources must use CloudPub resource auth, for example:

```bash
docker-compose --env-file .env.edge -f docker-compose.edge-cloudpub.synology.yml \
  run --rm edge-cloudpub register \
  --name lightrag-personal-memory \
  --auth basic \
  http lightrag-personal:9621

docker-compose --env-file .env.edge -f docker-compose.edge-cloudpub.synology.yml \
  run --rm edge-cloudpub register \
  --name lightrag-limbal-memory \
  --auth basic \
  http lightrag-project-limbal:9621
```

After registration, start the edge agent:

```bash
docker-compose --env-file .env.edge -f docker-compose.edge-cloudpub.synology.yml up -d
```

## Credentials

Do not commit CloudPub tokens, generated public URLs, or Basic Auth credentials.
Store them locally under `/Users/evgenybabenko/_CREDS/` and keep the files
`chmod 600`.

## Project Boundaries

Limbal owns `limbal-public-gateway` and the updater artifacts directory.
LightRAG owns `lightrag-personal` and `lightrag-project-limbal`.
The shared edge owns CloudPub state and public resource registration.
