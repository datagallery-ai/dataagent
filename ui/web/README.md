# Data Task UI (`@datagallery/web`)

Next.js data-task workspace in the datagallery monorepo.

- **User guide:** [Web 工作台指南](../../docs/zh/guides/web-workbench.md)
- **Quick start:** [快速开始](../../docs/zh/quick-start.md)

From the repository root:

```bash
npm install --include=dev
npm run build:web
npm run start:web
```

`npm run start:web` starts only the web app and proxies to `config.yaml` `server.host` and `server.port`, default `http://127.0.0.1:8790`. `npm run start` starts the control plane and the web app together.

Open <http://127.0.0.1:3000/data-tasks>.
