# Odoo CMC GitOps repository rules

- Repository này là nguồn desired state duy nhất; không thêm Odoo source hoặc build
  context vào đây.
- Workflow thường ngày không được `kubectl apply` workload. Chỉ workflow bootstrap
  thủ công được cài Argo CD, tạo runtime Secrets và apply AppProject/root Application.
- Mọi release image dùng tag `sha-<git-sha>`. App CI chỉ cập nhật `overlays/dev`;
  promotion Prod copy image đã kiểm thử sang `overlays/prod`, không rebuild.
  `overlays/cmc` là legacy migration; `overlays/cmc-common` không chứa image pins.
- CMC dùng PostgreSQL DBaaS 16 qua Secret `odoo-db-connection` do bootstrap quản lý.
  Không commit endpoint/credential và không thêm lại PostgreSQL StatefulSet/Service vào
  overlay CMC; base StatefulSet chỉ dành cho local.
- Odoo filestore/sessions cần RWX và UID/GID 101. Không thêm Redis làm session store.
- Backup cloud phải upload CMC S3 và chỉ được xem là hoàn chỉnh khi có `_SUCCESS` sau
  checksum. Không mô tả PVC backup cùng cluster là off-site DR.
- Production traffic đi CMC ELB private TCP 443 -> APISIX TLS -> Odoo stable/canary; giữ route WebSocket
  8072, upload 100 MiB và timeout 600 giây.
- Firewall và DNS do người dùng quản lý, không sửa trong repo. Không public ELB/IP.
- Dev chỉ ClusterIP + localhost port-forward qua bastion. Không đưa route Dev vào APISIX.
- Cùng cluster, namespace odoo-dev/odoo-prod và required node affinity + taint/toleration
  chính xác theo `odoo.cmc/environment`. Tính tổng budget trên DBaaS dùng chung.
- Không xóa/đổi destination legacy Application để migrate. Xem `cmc/MULTI-ENV.md`;
  giữ namespace/PVC, dừng writer legacy trước Prod mới, không dùng chung filestore giữa env.
- Sau mọi thay đổi manifest, chạy `python validate.py`, unit tests và
  `python tests/validate_cloud_charts.py`. Không tuyên bố CMC-ready nếu chưa smoke-test
  cluster, DBaaS, S3, NFS RWX, ELB/TLS và restore drill thật.
