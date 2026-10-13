# Trạng thái triển khai Odoo CMC GitOps

Cập nhật 2026-10-13: phần source đã hoàn tất theo two-repository pattern.

## Đã hoàn tất

- App repo chỉ giữ source, addons, Dockerfiles và CI; không còn bản sao manifest K8s.
- CI kiểm tra PR, build image chứa đúng source checkout, smoke-test rồi mới push hai
  image tag `sha-<commit>` và commit promotion sang repo này.
- Repo này là desired state duy nhất; Argo CD root theo dõi `clusters/cmc`.
- CMC overlay dùng PostgreSQL DBaaS qua Service/EndpointSlice, NFS RWX cho
  filestore/sessions và CMC ELB HTTPS cho APISIX.
- Backup nhất quán được lưu staging PVC và upload CMC S3; restore có thể tải lại từ
  S3 và bắt buộc kiểm tra marker/checksum.
- Runtime credentials/TLS không nằm trong Git; workflow bootstrap tạo Secret từ
  GitHub Actions Secrets trên bastion.
- Validation gồm Kustomize cross-resource checks, unit tests, Helm render và CRD
  schema checks.

## Cần dữ liệu/hạ tầng thật để nghiệm thu

- Điền domain, DBaaS private IP, StorageClass, S3 endpoint/bucket và ELB flavor.
- Cấu hình GitHub Secrets/Variable và self-hosted runner trên bastion.
- Xác nhận Security Group TCP 5432, NFS RWX UID/GID 101 và egress HTTPS tới S3.
- Chạy image pipeline, bootstrap Argo CD và smoke-test trên CMC.
- Chạy backup thủ công + restore drill trước khi bật CronJob định kỳ.
- Nghiệm thu canary traffic, KEDA, WebSocket, multi-node filestore/session và DBaaS
  failover. Trước production cần HA cho APISIX etcd và monitoring/alerting.

Runbook chi tiết: [`cmc/README.md`](cmc/README.md).
