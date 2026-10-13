# CMC Cloud qua Argo CD

CMC dùng GitOps. Git là nguồn trạng thái mong muốn; Argo CD đọc nhánh `main` và
đồng bộ cluster. GitHub Actions không chạy `kubectl apply` cho Odoo. Self-hosted
runner trên bastion chỉ dùng kubeconfig để cài Argo CD lần đầu và đăng ký root
Application.

```text
push code -> GitHub Actions build -> Docker Hub (sha-<commit>)
          -> commit cập nhật Kustomize -> Argo CD sync
          -> Argo Rollouts canary 20% -> 50% -> 100%
```

Argo CD quản lý trạng thái GitOps. Argo Rollouts điều khiển canary của web Odoo;
đây là hai controller khác nhau.

## Thành phần

- `.github/workflows/k8s-validate.yml`: kiểm tra manifest, chart và test.
- `.github/workflows/release-images.yml`: build hai image, push Docker Hub bằng tag
  bất biến rồi commit tag vào overlay CMC.
- `.github/workflows/bootstrap-argocd.yml`: chạy thủ công trên bastion để cài Argo
  CD và root Application.
- `k8s/gitops/bootstrap/`: AppProject và root Application.
- `k8s/gitops/applications/`: app-of-apps cho APISIX, Argo Rollouts, KEDA,
  APISIX etcd và Odoo.
- `k8s/overlays/cmc/`: trạng thái Odoo mà Argo CD theo dõi.

PostgreSQL và APISIX etcd hiện là single-instance. Cấu hình phù hợp staging; NFS,
PostgreSQL và etcd đơn vẫn là điểm lỗi đơn trước khi lên production.

## 1. Chuẩn bị GitHub và bastion

Trong repository GitHub, tạo:

- Secret `DOCKERHUB_USERNAME`.
- Secret `DOCKERHUB_TOKEN` là Docker Hub access token.
- Variable `CMC_KUBE_CONTEXT`, đúng bằng một tên từ `kubectl config get-contexts`
  trên bastion.
- Bật **Settings > Actions > General > Workflow permissions > Read and write
  permissions** để workflow commit tag image. Nếu `main` có branch protection,
  cho bot quyền phù hợp hoặc chuyển promotion thành pull request.

Runner bastion phải là Linux, có label `self-hosted`, `Linux`, `X64`, cùng
`kubectl`, Helm 3 và `openssl`. Kubeconfig của user chạy runner phải chứa context
CMC. Workflow truyền `--context` cho từng lệnh và không đổi context mặc định.

## 2. Điền cấu hình GitOps

Thay toàn bộ placeholder rồi commit lên `main`:

| Giá trị | File |
|---|---|
| `REPLACE_ODOO_DOMAIN` | `k8s/overlays/cmc/kustomization.yaml` |
| `REPLACE_CMC_BLOCK_STORAGE_CLASS` | overlay CMC và CMC infrastructure |
| `REPLACE_CMC_RWX_STORAGE_CLASS` | `k8s/overlays/cmc/kustomization.yaml` |
| `REPLACE_CMC_ELB_FLAVOR_ID` | `k8s/gitops/applications/apisix.yaml` |

Tên Docker Hub và tag image trong overlay được `release-images.yml` điền sau lần
build đầu. Bootstrap sẽ dừng nếu còn chuỗi `REPLACE_`.

`domain` là hostname người dùng truy cập, ví dụ `erp.example.vn`; sau khi APISIX
tạo ELB, DNS phải trỏ hostname này đến ELB. `CMC_KUBE_CONTEXT` là tên kết nối trong
kubeconfig, không phải tên hiển thị tùy ý trên CMC.

PostgreSQL và etcd dùng Block StorageClass (RWO). Filestore, session, backup và
restore dùng NFS StorageClass hỗ trợ RWX. NFS phải cho UID/GID 101 ghi được và cần
thử đọc/ghi từ pod trên ít nhất hai worker node.

## 3. Tạo TLS Secret

Bootstrap tự sinh `odoo-secrets` và `apisix-admin-credentials` nếu chưa có.
Certificate phải tạo trước:

```bash
CTX='ten-context-cmc'
kubectl --context "$CTX" create namespace odoo-system --dry-run=client -o yaml | kubectl --context "$CTX" apply -f -
kubectl --context "$CTX" -n odoo-system create secret tls odoo-tls \
  --cert=/path/fullchain.pem --key=/path/private.key
```

Không commit password, private key hoặc Docker config. Nếu Docker Hub repository
là private, tạo pull Secret trong `odoo-system` và thêm `imagePullSecrets` vào
Kustomize trước khi bootstrap.

## 4. Build image và bootstrap

Chạy workflow **Build images and promote GitOps** bằng `workflow_dispatch`. Nó tạo:

- `docker.io/<user>/odoo-queue:sha-<commit>`
- `docker.io/<user>/odoo-backup:sha-<commit>`

Sau khi commit promotion lên `main`, chạy **Bootstrap Argo CD on CMC**. Workflow:

1. xác minh tool, kube-context và placeholder;
2. tạo namespace cùng hai Secret ngẫu nhiên nếu cần;
3. yêu cầu `odoo-tls` đã tồn tại;
4. cài Argo CD chart `10.10.3`;
5. apply AppProject và root Application duy nhất.

Sau đó Argo CD quản lý APISIX `2.10.0`, Argo Rollouts `2.43.6`, KEDA `2.18.0`,
etcd và Odoo. Mọi thay đổi manifest cloud phải đi qua Git.

## 5. Theo dõi và đăng nhập

```bash
CTX='ten-context-cmc'
kubectl --context "$CTX" -n argocd get applications.argoproj.io
kubectl --context "$CTX" -n odoo-system get pods,pvc,rollouts,scaledobjects
kubectl --context "$CTX" -n apisix get service apisix-gateway
kubectl --context "$CTX" -n odoo-system get secret odoo-secrets \
  -o jsonpath='{.data.ADMIN_LOGIN_PASSWORD}' | base64 -d; echo
```

Đăng nhập `admin` tại `https://<domain>/web`. Nếu đổi password trong UI, Secret
vẫn giữ password khởi tạo. Mở Argo CD UI tạm thời bằng:

```bash
kubectl --context "$CTX" -n argocd port-forward service/argocd-server 8080:443
```

## 6. Phát hành và rollback

Khi image source thay đổi trên `main`, CI build/push rồi commit tag mới. Argo CD
đồng bộ Rollout; Argo Rollouts chuyển traffic APISIX qua 20%, 50%, 100%.

```bash
kubectl --context "$CTX" -n odoo-system get rollout odoo-web-rollout -w
```

Rollback bằng cách revert commit `chore(gitops): promote ...` rồi push `main`.
Có thể abort canary lỗi, nhưng image cuối cùng vẫn phải được phản ánh trong Git.

## 7. Kiểm tra trước cloud

`cmc.py` chỉ render và preflight chỉ đọc; nó không deploy workload:

```bash
python -m pip install -r k8s/requirements-tools.txt
python k8s/cmc.py render --config k8s/.local/cmc.yaml
python k8s/cmc.py preflight --config k8s/.local/cmc.yaml
python k8s/validate.py
python -m unittest discover -s k8s/tests -v
python k8s/tests/validate_cloud_charts.py
```

`k8s/cmc/config.example.yaml` dùng cho preflight và render chẩn đoán. Hãy điền các
giá trị tương ứng với overlay; nguồn triển khai thực tế vẫn là `k8s/overlays/cmc`.

Backup CronJob mặc định `suspend: true`. Chỉ bật sau khi backup thủ công và restore
drill thành công. Production cần bản sao object storage ngoài cluster và kiểm thử
phục hồi định kỳ.
