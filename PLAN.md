# KẾ HOẠCH TRIỂN KHAI HOÀN CHỈNH: ODOO DECOMPOSED MONOLITH TRÊN KUBERNETES

> Bản kế hoạch ban đầu, giữ để tham khảo lịch sử. Trạng thái thực thi hiện tại
> nằm tại [plan trung tâm](../.agents/plans/k8s-decomposed-monolith-plan.md).
> Khi triển khai CMC dùng [runbook cloud](cmc/README.md); các giá trị pool,
> replica và đường dẫn cũ dưới đây không phải cấu hình vận hành hiện tại.
> **Dự án**: Final Lab - Đưa Odoo ERP lên Kubernetes (CMC Cloud Standard)  
> **Mục tiêu**: Chuẩn hóa toàn bộ cấu trúc hạ tầng từ Local (Docker Desktop) đến Cloud, khắc phục triệt để các lỗi kỹ thuật thực tế, đồng bộ 100% mã nguồn Odoo và chuẩn bị tài liệu/luận điểm bảo vệ xuất sắc trước hội đồng.

---

## 1. TỔNG QUAN KIẾN TRÚC MỤC TIÊU (DECOMPOSED MONOLITH)

```
                            [ INTERNET / CLIENTS ]
                                       │
                                       ▼
                       [ Apache APISIX Gateway ]
                         - Modern API Gateway & Ingress
                         - Websocket Forwarding (/websocket -> 8072)
                         - Buffer 100MB cho import/file upload
                         - Argo Rollouts Canary Traffic Routing
                                       │
                      ┌────────────────┴────────────────┐
                      ▼ (HTTP Traffic)                  │
           [ Odoo Web Tier (Stateless) ]                │ (Internal Traffic Only)
           - Replicas: 2 -> 10 Pods                     ▼
           - Args: --max-cron-threads=0        [ Odoo Worker Tier (Async Queue) ]
           - KEDA ScaledObject (HTTP/CPU)      - OCA queue_job Job Runner
           - Liveness/Readiness: /web/health   - KEDA ScaledObject (Queue Depth)
                      │                        - Cron Runner (SKIP LOCKED Safe)
                      │                                 │
                      ├────────────────┬────────────────┤
                      ▼                ▼                ▼
            [ Shared Storage (PVC) ]   │     [ PostgreSQL 15 (DBaaS) ]
            - /var/lib/odoo/filestore  │     - max_connections: 300
            - /var/lib/odoo/sessions   │     - pg_notify (Bus Realtime)
            - fsGroup: 101             │     - Bảng queue_job & ir_cron
```

---

## 2. NỘI DUNG CÁC BƯỚC THỰC HIỆN CHI TIẾT

### Giai đoạn 1: Sửa các lỗi vận hành & Chuẩn hóa Hạ tầng Cơ sở (A4 - A9)

1. **Khắc phục quyền ghi Storage (Permission Denied A8)**:
   - Bổ sung `securityContext: { fsGroup: 101, runAsUser: 101 }` vào cả Web và Worker Pods để user `odoo` (UID 101) có toàn quyền ghi vào thư mục `/var/lib/odoo/filestore` và `/var/lib/odoo/sessions`.
2. **Cân đối Bộ nhớ & Tránh OOMKilled (A4)**:
   - Điều chỉnh cấu hình trong `odoo.conf`:
     - `limit_memory_soft = 805306368` (~768MB / worker)
     - `limit_memory_hard = 1073741824` (~1GB / worker)
   - Điều chỉnh Container Limits:
     - Web Pod (2 workers + 1 gevent + 1 master): Request `1Gi`, Limit `2.5Gi`.
     - Worker Pod: Request `512Mi`, Limit `1.5Gi`.
     - Ngăn ngừa tình trạng Linux Kernel OOMKill pod trước khi Odoo kịp tái chế tiến trình.
3. **Cấu hình Kết nối PostgreSQL & db_maxconn (A9)**:
   - Sửa `03-postgres.yaml`: Bổ sung tham số khởi động `-c max_connections=300` và `shared_buffers=256MB`.
   - Trong `odoo.conf`: Điều chỉnh `db_maxconn = 16` để tránh cạn kiệt connection pool khi KEDA scale lên 10 Web Pods.
4. **Bảo toàn Docker Entrypoint & Bí mật DB (A5)**:
   - Không ghi đè mù quáng `command: ["odoo", ...]`. Chuyển sang cấu hình chuẩn qua `args:` hoặc entrypoint script để giữ nguyên cơ chế `wait-for-psql.py` và nạp biến môi trường an toàn từ Kubernetes Secret.

---

### Giai đoạn 2: Tái cấu trúc Theo Các Quyết định Kỹ thuật (A1, A2, A3)

1. **Gỡ bỏ Redis, chuẩn hóa Native Session & Bus (Quyết định A3)**:
   - Xóa bỏ `04-redis.yaml` khỏi `kustomization.yaml`.
   - Khẳng định trong kiến trúc: Odoo lưu session an toàn trên Shared Storage (`FilesystemSessionStore`) và Bus đồng bộ thời gian thực qua PostgreSQL `pg_notify`.
2. **Tích hợp Apache APISIX Gateway (Quyết định A1)**:
   - Thay thế `08-ingress.yaml` (ingress-nginx cũ) bằng manifest APISIX Ingress / ApisixRoute:
     - Định tuyến `/websocket` sang cổng 8072.
     - Định tuyến `/` sang cổng 8069.
     - Cấu hình Timeout 600s và Max Body Size 100MB.
   - Cung cấp lệnh cài đặt APISIX Gateway bằng Helm vào `deploy.ps1`.
3. **Triển khai Hàng đợi Bất đồng bộ OCA `queue_job` (Quyết định A2)**:
   - Cấu hình server environment cho Worker Pod hỗ trợ chạy jobrunner:
     - Tham số nạp module `--load=base,web,queue_job`
     - Kênh xử lý jobrunner cấu hình qua biến môi trường.
4. **Cấu hình KEDA Autoscaling Event-Driven (A2 & KEDA)**:
   - Cập nhật `09-keda-autoscaling.yaml`:
     - **Web Scaler**: Scale từ 2 đến 10 Pods theo CPU/Memory và HTTP request rate.
     - **Worker Scaler**: Scale theo độ dài hàng đợi thực tế trong PostgreSQL thông qua trigger query:
       ```sql
       SELECT count(*) FROM queue_job WHERE state IN ('pending', 'enqueued')
       ```
5. **Chuẩn hóa Argo Rollouts Canary (A3/Rollouts)**:
   - Sửa `10-argo-rollout.yaml`:
     - Định nghĩa rõ ràng `stableService` (`odoo-web-stable`) và `canaryService` (`odoo-web-canary`).
     - Tích hợp traffic routing với APISIX để phân tách 20% -> 50% -> 100% không gián đoạn.
6. **Hoàn thiện Kế hoạch Disaster Recovery & Restore (A6)**:
   - Sửa `11-backup-cronjob.yaml`: Nhắm đúng database nghiệp vụ (`odoo17_dev`), sinh checksum SHA256 và lưu vào PVC backup tách biệt.
   - Bổ sung tài liệu / script kiểm thử diễn tập khôi phục (DR Restore Drill).

---

## 3. KẾ HOẠCH BÀN GIAO & DANH SÁCH FILE THỰC HIỆN

| STT | File | Hành động | Mục đích |
|---|---|---|---|
| 1 | `k8s/01-secrets-and-configmap.yaml` | Cập nhật | Tối ưu memory limits, connection pool, proxy_mode |
| 2 | `k8s/02-storage.yaml` | Cập nhật | Chuẩn hóa PVC filestore & backup volume |
| 3 | `k8s/03-postgres.yaml` | Cập nhật | Tăng `max_connections=300`, tối ưu tài nguyên |
| 4 | `k8s/04-redis.yaml` | Xóa/Loại bỏ | Loại bỏ Redis khỏi kustomization theo quyết định A3 |
| 5 | `k8s/05-odoo-web.yaml` | Cập nhật | Bổ sung `securityContext fsGroup`, memory sizing, probes |
| 6 | `k8s/06-odoo-worker.yaml` | Cập nhật | Cấu hình queue_job runner, fsGroup, memory sizing |
| 7 | `k8s/07-service.yaml` | Cập nhật | Tạo stable/canary services phục vụ Argo Rollouts |
| 8 | `k8s/08-apisix-gateway.yaml` | Tạo mới/Thay thế | Apache APISIX Gateway thay thế Ingress-NGINX |
| 9 | `k8s/09-keda-autoscaling.yaml` | Cập nhật | Bổ sung PostgreSQL Scaler đo độ dài queue_job |
| 10 | `k8s/10-argo-rollout.yaml` | Cập nhật | Hoàn chỉnh spec canary với APISIX traffic routing |
| 11 | `k8s/11-backup-cronjob.yaml` | Cập nhật | Nhắm đúng DB Odoo, bổ sung restore script |
| 12 | `k8s/kustomization.yaml` | Cập nhật | Đồng bộ danh sách tài nguyên mới |
| 13 | `k8s/deploy.ps1` | Cập nhật | Script triển khai 1-click mượt mà cho Docker Desktop |
| 14 | `k8s/README.md` | Viết lại hoàn chỉnh | Cẩm nang kiến trúc & Luận điểm bảo vệ đồ án CMC Cloud |
| 15 | `AGENTS.md` | Tạo mới | Quy chuẩn làm việc, context và prompt mẫu cho AI/Codex |
