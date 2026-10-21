# GitOps: main / dev / prod

Quy trình hiện hành từ 2026-10-18; thay thế hướng dẫn deploy `both`/mọi overlay trên
`main` trong các runbook cũ. Không đổi namespace, node group, DB, PVC hay firewall.

| Nhánh GitOps | Argo đọc gì | Khi cập nhật |
|---|---|---|
| main | root, platform, định nghĩa Application/AppProject | Argo platform auto-sync |
| dev | overlays/dev | Dev auto-sync/self-heal; push workload chạy Deploy Dev để nghiệm thu |
| prod | overlays/prod | không auto-sync; Deploy Prod hoặc promotion có approval |

Repo ứng dụng `erp-odoo-cmccloud` vẫn dùng main để build. CI của nó chỉ push image pins
vào **GitOps dev**, không push main/prod. Hai nhánh mới thuộc repo `odoo-cmc-gitops`.
Không cần hai bản sao Argo hoặc hai ELB. Các file không thuộc phạm vi nhánh vẫn có trong
checkout, nhưng Argo không đọc chúng từ nhánh đó.

Tham khảo workflow [CI Vote](https://github.com/hgiang25/aws-eks-gitops-platform/blob/main/.github/workflows/ci-vote.yaml):
workflow mẫu chọn environment/version nhưng vẫn push GitOps main. Branch-per-environment
là phần mở rộng theo yêu cầu này, không phải khẳng định repo mẫu cũng làm như vậy.

## Workflow nào chạy khi nào?

| Workflow | Nguồn | Quyền tác động |
|---|---|---|
| Validate Kubernetes and GitOps | PR/push main, dev, prod | validate, không deploy |
| Configure CMC desired state | thủ công main | sửa platform/config trên main; không tự deploy Dev/Prod |
| Bootstrap CMC environment | thủ công main, chọn dev HOẶC prod | lần đầu/rotate Secret; platform main + workload nhánh đã chọn |
| Deploy Dev | push workload dev, dispatch CI, hoặc thủ công main/dev | chỉ sync odoo-dev và verify |
| Deploy Prod | thủ công main/prod | chỉ sync odoo-prod và verify, GitHub environment odoo-prod |
| Promote latest Dev images to prod | Run workflow on main, no inputs | Resolve Dev HEAD -> verify -> Prod approval -> publish release tags -> commit prod -> sync Prod |
| Verify CMC reconciliation | thủ công main, chọn một môi trường | chỉ kiểm tra, không sync |

Daily deploy không cài lại Argo, không ghi Secret, không apply workload bằng kubectl.
Reusable workflow dùng code điều phối từ main, checkout workload riêng, kiểm tra đúng
branch HEAD. Platform và workload có SHA khác nhau là bình thường. Verify so sánh mỗi
Application với SHA tương ứng, không ép platform bằng SHA prod/dev.
PR/push validation vẫn phải là required check; Dev auto-sync có thể xảy ra trước CI
trên push, nên bảo vệ nhánh và merge PR đã pass thay vì push cấu hình chưa kiểm thử.

## Thiết lập GitHub bắt buộc (người quản trị)

- Branch protection/rulesets cho main/dev/prod: PR review, required validation checks,
  không force-push/delete. Cho phép bot promotion theo policy riêng nếu cần.
- Settings -> Environments -> odoo-prod: required reviewers, prevent self-review nếu
  có; cho phép deployment từ main/prod (promotion workflow bắt đầu trên main).
- odoo-dev: cho phép main/dev. Runner vẫn dùng kubeconfig có sẵn, không thêm secret kubeconfig.
- YAML `environment: odoo-prod` KHÔNG tự tạo required reviewers. Argo manual Sync cũng
  có thể bỏ qua GitHub approval; giới hạn quyền sync Prod bằng Argo RBAC nếu cần cưỡng chế.
- Không chia sẻ quyền ghi prod với mọi developer. Dữ liệu DB và quyền cloud không thay đổi.

## Chuyển đổi lần này

1. Tạo dev và prod từ cùng commit đã kiểm thử, trước hoặc đồng thời với main đổi
   targetRevision. Không để Application trỏ tới nhánh chưa tồn tại.
2. Root main sẽ đổi odoo-dev.source.targetRevision=dev và odoo-prod=prod.
   Manifest workload ban đầu giữ nguyên, không đổi image/database.
3. Chờ root nhận cấu hình, Dev Synced/Healthy. Chạy Deploy Prod trên main nếu Prod cần sync.
4. Chạy Verify CMC reconciliation cho từng môi trường. Runner vẫn cần DNS/route/TLS Prod.

## Release mới

1. Merge code ứng dụng vào main -> CI build hai image sha-* -> cập nhật GitOps dev.
2. Argo Dev reconcile; Deploy Dev verify revision, pods, placement và HTTP/DB.
3. Run **Promote latest Dev images to prod** on `main`; no SHA or image tag inputs.
   The workflow snapshots the latest `dev` HEAD and reads both image pins from
   `overlays/dev/kustomization.yaml`. Dev must pass verification at that revision.
   If Dev advances before verification, rerun; after verification the accepted
   snapshot remains pinned even if Dev subsequently changes.
4. The first Prod version is `v1.0.0` when its current tag is `sha-*`. Subsequent
   runs increment the current Prod patch: `v1.0.1`, `v1.0.2`, etc. Each successful
   run allocates a new version, including when Dev images have not changed.
5. Docker Buildx tags the same source digests without rebuilding, verifies both
   destination digests, then updates only the Prod image pair and deploys it.
   Prod configuration, database schema and data are not copied from Dev.
   The commit and Actions Summary record the Dev revision, source tag and release.
6. Configure `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` in this GitOps repository
   or its `odoo-prod` environment, with pull/push access to both image repositories.
   Application-repository secrets are not automatically shared with GitOps.
7. Existing release tags with different digests cause failure, never overwrite.
   Retrying a partial registry publication reuses matching tags. Authentication or
   network failures are not treated as missing tags. After rolling Prod back,
   resolve any occupied next version in Git before retrying. Concurrent Prod Git
   changes cause push failure instead of rebasing an allocated release version.


## Thay đổi cấu hình dùng chung

Base/cmc-common được snapshot riêng theo branch. Thay đổi trên main KHÔNG tự đổi workload
dev/prod. Đưa commit cấu hình được review sang dev bằng PR/cherry-pick, test, rồi đưa cùng
thay đổi cần thiết sang prod qua PR. Không copy cả overlays/dev vào overlays/prod.
Configure workflow chỉ ghi main; chuyển phần cấu hình chung cần thiết tới hai branch bằng
PR. Chạy validate trên cả hai snapshot để tránh vượt DB budget chung:

```bash
python scripts/validate_branch_pair.py --dev-root /path/to/dev-checkout --prod-root /path/to/prod-checkout
```

Daily workflows cũng kiểm tra budget trên hai branch thật, thay vì sibling overlay có
thể đã cũ. Giữ các thay đổi maxReplicaCount/db_maxconn tuần tự, verify sau từng lần;
hai branch vẫn dùng chung DBaaS nên không có isolation tài nguyên tuyệt đối.

## Demo dễ quan sát

Sửa quota trong **nhánh dev**, overlays/dev/boundaries.yaml; commit/PR vào dev.
Argo Dev syncs independently of Prod. See [DEMO-DEV-PROD.md](DEMO-DEV-PROD.md).
After promotion, Dev uses `sha-*` and Prod uses `vX.Y.Z`, with matching image digests.
Do not run bootstrap for each release.
