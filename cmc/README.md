# Runbook triển khai Odoo trên CMC Cloud

Luồng triển khai là pull-based GitOps:

```text
push/merge app -> CI build và smoke-test -> Docker Hub sha-<commit>
               -> commit tag vào repo GitOps -> Argo CD sync
               -> Argo Rollouts chuyển traffic APISIX 20% -> 50% -> 100%
```

Self-hosted runner trên bastion chỉ cài Argo CD, tạo runtime Secrets và đăng ký root
Application. Sau bootstrap, không dùng runner hoặc `kubectl set image` để deploy Odoo.

## 1. Tài nguyên CMC cần có

- CMC Kubernetes có ít nhất hai worker Ready và Metrics API hoạt động.
- NFS StorageClass hỗ trợ `ReadWriteMany`; UID/GID `101` ghi được từ nhiều node.
- Block StorageClass cho APISIX etcd staging.
- PostgreSQL DBaaS 15 trong cùng VPC/subnet có private IP cố định hoặc VIP (backup
  image hiện ghim `pg_dump` 15, nên không trỏ nó vào server major mới hơn).
  User Odoo cần quyền tạo database trong lần init; đặt `max_connections >= 300` bằng
  Configuration Group vì GitOps không thể chỉnh tham số của DBaaS.
- CMC S3 bucket; nên bật versioning, lifecycle và Object Lock nếu gói hỗ trợ.
- Domain và certificate TLS hợp lệ.
- Security Group cho phép worker/node CIDR và KEDA kết nối DBaaS TCP 5432; cho phép
  egress HTTPS tới S3 endpoint. Kubernetes NetworkPolicy không bảo vệ DBaaS bên ngoài
  cluster, nên Security Group là lớp bắt buộc.

CMC hỗ trợ Service `LoadBalancer` qua các annotation
`loadbalancer.openstack.org/class` và `loadbalancer.openstack.org/flavor-id`. Overlay
chỉ mở listener TLS 443 của APISIX; HTTP không được publish.

## 2. Điền desired state

Thay mọi placeholder rồi commit vào `main`:

| Placeholder | File | Giá trị |
|---|---|---|
| `REPLACE_ODOO_DOMAIN` | `overlays/cmc/kustomization.yaml` | Ví dụ `erp.example.vn` |
| `REPLACE_CMC_DBAAS_PRIVATE_IP` | `overlays/cmc/dbaas-endpoint.yaml` | Private IP/VIP PostgreSQL |
| `REPLACE_CMC_S3_ENDPOINT_URL` | `overlays/cmc/kustomization.yaml` | HTTPS endpoint đúng region |
| `REPLACE_CMC_S3_BUCKET` | `overlays/cmc/kustomization.yaml` | Bucket backup đã tạo |
| `REPLACE_CMC_RWX_STORAGE_CLASS` | `overlays/cmc/kustomization.yaml` | NFS RWX StorageClass |
| `REPLACE_CMC_BLOCK_STORAGE_CLASS` | `overlays/cmc-infrastructure/kustomization.yaml` | Block StorageClass |
| `REPLACE_CMC_ELB_FLAVOR_ID` | `clusters/cmc/apisix.yaml` | Flavor ID đúng region |

`REPLACE_DOCKERHUB_USERNAME` và `REPLACE_IMAGE_TAG` được pipeline app thay sau lần
publish đầu tiên. Bootstrap từ chối chạy nếu bất kỳ `REPLACE_` nào còn tồn tại.

DBaaS được ánh xạ vào DNS nội bộ `postgres-service.odoo-system.svc.cluster.local`
bằng headless Service + EndpointSlice. Vì vậy Odoo, init Job, backup và KEDA dùng cùng
một tên kết nối; đổi private IP chỉ cần một commit GitOps.

## 3. Cấu hình GitHub

Repo ứng dụng `erp-odoo-cmccloud`:

- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`
- `GITOPS_PAT`: fine-grained token chỉ có `Contents: Read and write` trên
  `odoo-cmc-gitops`

Repo GitOps này:

- Variable `CMC_KUBE_CONTEXT`
- Secret `CMC_POSTGRES_USER`
- Secret `CMC_POSTGRES_PASSWORD`
- Secret `CMC_S3_ACCESS_KEY_ID`
- Secret `CMC_S3_SECRET_ACCESS_KEY`
- Secret `CMC_TLS_CERTIFICATE`: full chain PEM
- Secret `CMC_TLS_PRIVATE_KEY`: private key PEM

Gắn self-hosted runner Linux trên bastion với labels `self-hosted`, `Linux`, `X64`;
runner cần `kubectl`, Helm 3 và `openssl`. Kubeconfig chỉ cấp quyền cần cho bootstrap
và các namespace của platform.

Workflow bootstrap không ghi secret vào Git. Nó tạo:

- `odoo-secrets`: DB credential từ GitHub Secrets; master/login password sinh ngẫu nhiên.
- `odoo-s3-credentials`: access key/secret key CMC S3.
- `odoo-tls`: certificate/key; chạy lại workflow sẽ rotate TLS và S3 credentials.
- `apisix-admin-credentials`: sinh một lần và giữ lại.

`odoo-secrets` không tự overwrite nếu đã tồn tại để tránh làm mất kết nối DB. Khi
rotate DB password, cập nhật DBaaS và Secret theo một maintenance procedure riêng.

## 4. Thứ tự phát hành lần đầu

1. Chạy workflow app **CI images and GitOps promotion** bằng `workflow_dispatch`.
2. Xác nhận commit `chore(gitops): promote ...` xuất hiện ở repo này.
3. Chạy các validation dưới đây và đảm bảo không còn placeholder.
4. Chạy workflow **Bootstrap Argo CD on CMC** trên bastion.
5. Chờ năm child Applications `Healthy/Synced`.
6. Lấy external IP của `service/apisix-gateway`, trỏ DNS domain vào ELB.
7. Smoke-test health, login, upload attachment, WebSocket, queue job và multi-node RWX.
8. Chạy backup thủ công + restore drill; sau khi pass mới đổi `suspend` thành `false`
   trong patch CronJob của `overlays/cmc/kustomization.yaml`.

```bash
python -m pip install -r requirements-tools.txt
python validate.py
python -m unittest discover -s tests -v
python tests/validate_cloud_charts.py
kubectl kustomize overlays/cmc >/tmp/odoo-cmc.yaml

CTX='ten-context-cmc'
kubectl --context "$CTX" -n argocd get applications.argoproj.io
kubectl --context "$CTX" -n odoo-system get pods,pvc,rollouts,scaledobjects,endpointslices
kubectl --context "$CTX" -n apisix get service apisix-gateway
curl -fsS "https://erp.example.vn/web/health"
```

Đăng nhập user `admin`; lấy password khởi tạo:

```bash
kubectl --context "$CTX" -n odoo-system get secret odoo-secrets \
  -o jsonpath='{.data.ADMIN_LOGIN_PASSWORD}' | base64 -d; echo
```

## 5. Backup, S3 và restore

Backup dừng tạm thời web/worker/cron/runner để snapshot DB và filestore nhất quán,
sau đó luôn cố phục hồi replica/KEDA trong `finally`. Bộ backup gồm `database.dump`,
`filestore.tar.gz`, `metadata.json`, `SHA256SUMS`; chỉ sau khi upload đủ bốn file mới
ghi `_SUCCESS` lên `s3://<bucket>/odoo/backups/<UTC timestamp>/`.

```bash
kubectl --context "$CTX" -n odoo-system create job \
  --from=cronjob/odoo-disaster-recovery-backup manual-backup-001
kubectl --context "$CTX" -n odoo-system logs -f job/manual-backup-001
python cmc.py render --config .local/cmc.yaml
pwsh ./dr/restore-drill.ps1 -Context "$CTX" -BackupSet 20261013T190000Z \
  -Template .local/cmc-rendered/restore-job.yaml
```

Restore ưu tiên bản trên PVC; nếu không có, job tải bộ có marker `_SUCCESS` từ CMC S3
rồi kiểm tra SHA256 trước khi tạo database drill mới và xác minh attachment. CMC RDS
cũng nên bật PITR; S3 backup là lớp DR độc lập cho cả DB dump và Odoo filestore.

## 6. Rollback và giới hạn

Rollback release bằng cách revert commit promotion trong repo GitOps. Có thể abort
canary đang lỗi, nhưng version cuối cùng vẫn phải được phản ánh trong Git.

Thiết kế hiện tại phù hợp staging/lab: APISIX etcd vẫn một replica và backup làm Odoo
ngừng ghi trong thời gian snapshot. Trước production cần HA etcd, kiểm thử DBaaS failover,
monitoring/alerting, off-site retention, restore định kỳ và xác nhận support matrix của
phiên bản Kubernetes/controller trên CMC.
