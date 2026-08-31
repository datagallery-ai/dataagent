## Pull Docker Images

This is step 1 of the database installation flow. Use it to prepare Docker images in advance. After pulling images, continue with [Deploy Database Services](service-deployment.md) to start MySQL and PostgreSQL.

If `docker pull` is unstable due to network issues, retry a few times.

```bash
# Use a more stable mirror
# Pull PostgreSQL image
docker pull docker.m.daocloud.io/library/postgres:15-alpine

# Pull MySQL image
docker pull docker.m.daocloud.io/library/mysql:8.4

# After success, retag so docker-compose can recognize them
docker tag docker.m.daocloud.io/library/postgres:15-alpine postgres:15-alpine
docker tag docker.m.daocloud.io/library/mysql:8.4 mysql:8.4
```
