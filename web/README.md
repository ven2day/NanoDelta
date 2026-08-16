# NanoDelta Web UI

Authenticated operations UI for authoritative NanoDelta API data. The browser talks only to the
Next.js server-side gateway. Backend API keys are read from mounted files and are never bundled into
browser JavaScript.

See [UI authentication and API integration](../docs/UI_AUTH_AND_API.md) for setup, supported views,
role behavior, and known backend gaps.

```bash
npm install
npm run lint
npm run build
```
