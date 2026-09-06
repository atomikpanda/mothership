# syntax=docker/dockerfile:1
FROM golang:1.26.8-alpine@sha256:ce864e7223ac17b1775e6fd0b4c0db580c2eb50e7953a427916379e4b92a1628 AS source
WORKDIR /src
# Bound compiler concurrency and heap growth on small relay hosts.
ENV CGO_ENABLED=0 GOFLAGS=-p=1 GOMAXPROCS=2 GOMEMLIMIT=512MiB

# sish v2.23.0, pinned independently of mutable upstream image tags.
ADD --checksum=sha256:1efd323d9d5148211e4d0347d355cca2bd3b68fc89462b4af22ee4b272431a19 https://codeload.github.com/antoniomika/sish/tar.gz/7a53da3d988b809782175f9e62a7066e5a7dad31 /tmp/sish.tar.gz
RUN tar -xzf /tmp/sish.tar.gz --strip-components=1 -C /src && mkdir /emptydir
COPY checks/ssh-close.go /tmp/ssh-close.go

FROM source AS build
# v0.52.0 spins in SendRequest after SSH shutdown (golang/go#79717).
# Keep the security fixes; upgrade rather than disabling keepalives or downgrading.
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    go get golang.org/x/crypto@v0.53.0 && \
    go run /tmp/ssh-close.go && \
    go build -trimpath -o /out/app \
      -ldflags="-s -w -X github.com/antoniomika/sish/cmd.Version=v2.23.0-mship-crypto0.53.0 -X github.com/antoniomika/sish/cmd.Commit=7a53da3d988b809782175f9e62a7066e5a7dad31" .

FROM scratch
WORKDIR /app
LABEL org.opencontainers.image.title="mship sish relay" \
      org.opencontainers.image.version="v2.23.0-mship-crypto0.53.0" \
      org.opencontainers.image.source="https://github.com/antoniomika/sish" \
      org.opencontainers.image.revision="7a53da3d988b809782175f9e62a7066e5a7dad31"
COPY --from=build /emptydir /tmp
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
COPY --from=build /src/deploy/ /app/deploy/
COPY --from=build /src/README* /src/LICENSE* /app/
COPY --from=build /src/templates/ /app/templates/
COPY --from=build /out/app /app/app
ENTRYPOINT ["/app/app"]
