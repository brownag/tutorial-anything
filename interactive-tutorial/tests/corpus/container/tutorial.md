# Build and run a greeting container

1. Write the Dockerfile: `FROM alpine` with a `CMD` that prints `hello`.
2. Docker group: run `sudo usermod -aG docker "$USER"`, then log out and back in.
3. Build: `docker build -t greet:latest .`
4. Run: `docker run --rm greet:latest` prints `hello`.
