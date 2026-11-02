# DataLink

`dataagent serve` does not mount DataLink routes, and deploy does not start `services/datalink`.

If the Web DataLink panel calls `/api/v1/datalink/*`, the control plane returns 404. Run that service on its own, using the notes under `services/datalink`. It is not part of the current control plane.

For local files the model should read, use workspace directories in [Where data comes from](data-sources.md).
