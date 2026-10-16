> **Superseded 2026-10-15:** private Dev/Prod configuration and migration now follow [MULTI-ENV.md](MULTI-ENV.md). Firewall is already operational and remains user-managed. The historical instructions below describe the previous single-namespace state.

# Runbook triển khai Odoo trên CMC Cloud

Luồng phát hành là pull-based GitOps:

```text
push/merge app -> CI build và smoke-test -> Docker Hub sha-<commit>
               -> commit image tag vào repo GitOps -> Argo CD sync
               -> canary health analysis -> APISIX chuyển 20% -> 50% -> 100%
```

Self-hosted runner trên bastion chỉ cấu hình Secret runtime, kiểm tra DBaaS, bootstrap
Argo CD và chạy kiểm tra live. Sau bootstrap, Argo CD là thành phần duy nhất reconcile
workload Odoo.

## 1. Tài nguyên phải có trước khi deploy

- CMC Kubernetes có ít nhất hai worker `Ready` và Metrics API hoạt động.
- NFS/File StorageClass hỗ trợ `ReadWriteMany`; UID/GID `101` ghi được từ nhiều node.
- Block StorageClass cho APISIX etcd.
- PostgreSQL DBaaS **16** trong cùng VPC, có private IP hoặc hostname mà pod trong
  cluster truy cập được. User Odoo cần quyền tạo database trong lần init và DBaaS cần
  `max_connections >= 300`.
- Domain và certificate TLS hợp lệ.
- CMC ELB flavor ID đúng region.
- Security Group cho phép worker/node CIDR kết nối DBaaS TCP 5432.

S3, backup, restore và DR đang để ngoài phạm vi triển khai hiện tại. CronJob backup vẫn
`suspend`; các GitHub Secret S3 là tùy chọn và chưa cần tạo.

## 2. Placeholder là gì?

Các chuỗi `REPLACE_*` là **sentinel**, không phải Secret. Chúng giúp pipeline phát hiện
rằng cấu hình cloud thật chưa được cung cấp, thay vì vô tình deploy domain, StorageClass
hoặc ELB giả.

| Placeholder | Được thay bởi |
|---|---|
| `REPLACE_ODOO_DOMAIN` | GitHub Variable `ODOO_DOMAIN` |
| `REPLACE_CMC_RWX_STORAGE_CLASS` | `CMC_RWX_STORAGE_CLASS`; mặc định `nfs-csi` |
| `REPLACE_CMC_BLOCK_STORAGE_CLASS` | `CMC_BLOCK_STORAGE_CLASS` |
| APISIX gateway đang `ClusterIP` an toàn | `CMC_ELB_FLAVOR_ID`; Configure mới bật private ELB |
| `REPLACE_DOCKERHUB_USERNAME`, `REPLACE_IMAGE_TAG` | Pipeline release của app repo |

Không nên xóa placeholder trước khi có giá trị thật: manifest sẽ mất gate an toàn nhưng
vẫn không có cấu hình để chạy. Bạn không cần sửa YAML bằng tay. Sau khi điền GitHub
Variables, chạy workflow **Configure CMC desired state**; workflow validate rồi commit
các giá trị không nhạy cảm vào GitOps. Pipeline app sẽ thay hai placeholder image.

IP/hostname DBaaS không còn là placeholder trong Git. Bootstrap đọc nó từ GitHub Variable,
tạo Kubernetes Secret `odoo-db-connection`, và cả Odoo lẫn KEDA dùng chung Secret này.
Đổi endpoint hoặc credential chỉ cần cập nhật Variable/Secret rồi chạy lại bootstrap;
workflow sẽ rolling-restart các workload Odoo đang tồn tại để chúng đọc giá trị mới.

## 3. Cấu hình GitHub

### App repo `hgiang25/erp-odoo-cmccloud`

GitHub Secrets bắt buộc:

- `DOCKERHUB_USERNAME`: Docker Hub username.
- `DOCKERHUB_TOKEN`: Docker Hub access token, không dùng password tài khoản.
- `GITOPS_PAT`: fine-grained PAT có quyền `Contents: Read and write` trên
  `hgiang25/odoo-cmc-gitops`; cần thêm quyền gọi repository dispatch nếu bật verify tự động.

GitHub Variable tùy chọn:

- `CMC_AUTO_DEPLOY_DEV=true`: sau khi promote image sẽ gọi workflow deploy/verify Dev ở repo
  GitOps. Chỉ bật sau khi bootstrap cluster và self-hosted runner đã hoạt động.

### GitOps repo `hgiang25/odoo-cmc-gitops`

GitHub Variables bắt buộc:

- `CMC_KUBE_CONTEXT`: tên context CMC có sẵn trên bastion.
- `CMC_POSTGRES_HOST`: private IP hoặc hostname DBaaS, không kèm scheme/port.
- `ODOO_DOMAIN`: ví dụ `erp.example.vn`.
- `CMC_BLOCK_STORAGE_CLASS`: block StorageClass cho APISIX etcd.
- `CMC_ELB_FLAVOR_ID`: flavor ID đúng region CMC.

GitHub Variables có mặc định:

- `CMC_RWX_STORAGE_CLASS`: mặc định `nfs-csi`, đúng với StorageClass NFS hiện có;
  chỉ cần tạo Variable này nếu sau đó đổi tên StorageClass.
- `CMC_POSTGRES_PORT`: mặc định `5432`.
- `CMC_POSTGRES_DATABASE`: mặc định `odoo17_prod`.
- `CMC_POSTGRES_SSLMODE`: mặc định `disable` cho private endpoint DBaaS hiện tại đã xác nhận
  không hỗ trợ TLS; cho phép `disable`, `require`, `verify-ca`, `verify-full`. Khi dùng
  `disable`, phải giới hạn Security Group TCP 5432 cho đúng worker/node CIDR.

GitHub Secrets bắt buộc:

- `CMC_POSTGRES_USER`.
- `CMC_POSTGRES_PASSWORD`.
- `DOCKERHUB_USERNAME`.
- `DOCKERHUB_TOKEN`: dùng tạo `imagePullSecret`, hỗ trợ cả Docker Hub private repository.
- `CMC_TLS_CERTIFICATE`: full-chain PEM.
- `CMC_TLS_PRIVATE_KEY`: private key PEM.

GitHub Secrets tùy chọn, chỉ dùng khi hoàn thiện backup/DR sau này:

- `CMC_S3_ACCESS_KEY_ID`.
- `CMC_S3_SECRET_ACCESS_KEY`.

Không export password/IP bằng tay trong shell bastion. GitHub Actions truyền chúng vào
workflow; workflow tạo hoặc rotate Kubernetes Secret bằng `kubectl ... --dry-run=client |
kubectl apply -f -`. Secret không được commit vào Git.

## 4. Chuẩn bị self-hosted runner

Runner Linux x64 trên bastion phải có labels `self-hosted`, `Linux`, `X64` và các lệnh:

```bash
kubectl version --client
helm version
openssl version
curl --version
```

Kubeconfig trên runner phải có context đúng với `CMC_KUBE_CONTEXT`. Bastion và cluster
phải truy cập được private endpoint DBaaS. Repo và runner nên được bảo vệ bằng branch
protection, GitHub Environment approval và quyền Kubernetes tối thiểu cần thiết.

Nếu `kubectl` thỉnh thoảng báo `You must be logged in to the server (Unauthorized)` rồi
lệnh kế tiếp lại thành công, đây là lỗi xác thực kubeconfig/control plane, không phải lỗi
NFS. Không cài lại Helm release `csi-driver-nfs`. Kiểm tra từ bastion:

```bash
kubectl config current-context
kubectl auth can-i get nodes
for i in $(seq 1 10); do
  date -Is
  kubectl get --raw=/readyz || true
done
timedatectl status
```

Nếu tất cả lần gọi đều `Unauthorized`, tải/cấp lại kubeconfig từ CMC và cài nó cho đúng
user chạy GitHub Runner (kubeconfig của `root` không tự động dùng được bởi runner user).
Nếu kết quả lúc thành công lúc thất bại như log hiện tại, lưu endpoint cùng thời điểm lỗi
và yêu cầu CMC kiểm tra các API server phía sau load balancer; không dùng retry để che lỗi
xác thực trước khi bootstrap production.

NFS export dành riêng cho Odoo phải bảo đảm thư mục do CSI tạo có thể ghi bởi UID/GID
`101`. Với `root_squash`, cấu hình khuyến nghị trên NFS server là:

```exports
/home/nfs-pv 10.0.20.0/24(rw,sync,no_subtree_check,root_squash,anonuid=101,anongid=101,mountpoint)
```

Sau khi sửa, chạy `exportfs -rav` và xác nhận `exportfs -v` có `anonuid=101` cùng
`anongid=101`. Bootstrap tạo trước PVC thật `odoo-filestore-pvc`, chạy một Job tối thiểu
bằng UID/GID `101` để ghi/đọc/xóa marker trên NFS rồi mới kiểm tra DBaaS và cài Argo CD.
Nếu quyền NFS sai, pipeline dừng sớm và giữ PVC để quản trị viên sửa ownership; nó không
tiếp tục reconcile Odoo trong trạng thái filestore không ghi được.

## 5. Thứ tự triển khai lần đầu

1. Tạo DBaaS PostgreSQL 16, NFS RWX StorageClass, block StorageClass, domain/TLS và ELB.
   Cluster hiện tại đã có `nfs-csi` cho RWX và `highio` cho block storage; xác nhận lại bằng
   `kubectl get sc` trước khi chạy workflow cấu hình.
2. Điền GitHub Variables và Secrets ở mục 3.
3. Chạy app workflow **CI images and GitOps promotion** bằng `workflow_dispatch` để publish
   image bất biến và thay placeholder image.
4. Chạy GitOps workflow **Configure CMC desired state** để commit domain, StorageClass và
   ELB flavor.
5. Xác nhận không còn placeholder trong cấu hình deploy:

   ```bash
   grep -R "REPLACE_" overlays/cmc overlays/cmc-infrastructure clusters/cmc
   ```

6. Gắn self-hosted runner rồi chạy **Bootstrap Argo CD on CMC**.
7. Bootstrap tạo/rotate `odoo-db-connection`, `odoo-registry`, TLS Secret; sau đó chạy Job
   `postgres:16-alpine` trong cluster để kiểm tra DB version, TLS và credential.
8. Chờ năm Argo Applications `Healthy/Synced`, lấy external IP của `apisix-gateway` và
   trỏ DNS domain vào ELB.
9. Chạy workflow **Verify CMC reconciliation**; workflow đợi Argo, canary Rollout, xác nhận
   image tag và gọi `https://<domain>/web/health?db_server_status=1`.
10. Chỉ sau khi bước 9 pass mới đặt `CMC_AUTO_DEPLOY_DEV=true` ở app repo.

## 6. Kiểm tra offline

```bash
python -m pip install -r requirements-tools.txt
python validate.py
python -m unittest discover -s tests -v
python tests/validate_cloud_charts.py
kubectl kustomize overlays/cmc >/tmp/odoo-cmc.yaml
```

Kiểm tra live từ bastion:

```bash
CTX='ten-context-cmc'
kubectl --context "$CTX" -n argocd get applications.argoproj.io
kubectl --context "$CTX" -n odoo-system get pods,pvc,rollouts,analysisruns,scaledobjects
kubectl --context "$CTX" -n apisix get service apisix-gateway
curl -fsS 'https://erp.example.vn/web/health?db_server_status=1'
```

Đăng nhập user `admin`; lấy password khởi tạo:

```bash
kubectl --context "$CTX" -n odoo-system get secret odoo-secrets \
  -o jsonpath='{.data.ADMIN_LOGIN_PASSWORD}' | base64 -d; echo
```

## 7. Giới hạn còn lại

- Không tuyên bố CMC-ready trước khi test thật DBaaS, NFS RWX nhiều node, image pull,
  ELB/TLS, WebSocket, queue job, KEDA và canary analysis.
- APISIX etcd chạy ba member với PDB `minAvailable: 2`; production vẫn cần monitoring và
  diễn tập mất node/volume thực tế.
- Backup, restore, S3 retention và DR sẽ được thiết kế/nghiệm thu trong giai đoạn riêng.
- Phiên bản Kubernetes/controller phải khớp support matrix thực tế của CMC.
