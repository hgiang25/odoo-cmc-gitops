# Demo Dev/Prod qua Git va Argo CD

Quy trinh branch/workflow hien hanh: [BRANCH-WORKFLOW.md](BRANCH-WORKFLOW.md).

## Thiet ke

Tham khao [k8s-gitops](https://github.com/hgiang25/k8s-gitops): base dung chung,
overlay va Application rieng, Dev auto-sync/self-heal, Prod sync co kiem soat.
[aws-eks-gitops-platform](https://github.com/hgiang25/aws-eks-gitops-platform)
dung management/dev/prod clusters rieng. O day giu CMC **mot cluster**, tach namespace,
AppProject, DB role, PVC va worker groups; khong tu nhan co isolation nhu ba clusters.
Dung chung base hoac cung image sau promotion la dung: cau hinh va vong doi release
moi can doc lap. Khong tao tag gia hoac rebuild rieng cho Prod de tao khac biet.

| Thuoc tinh | Dev | Prod |
|---|---|---|
| Application / namespace / project | odoo-dev | odoo-prod |
| Desired state | dev:overlays/dev | prod:overlays/prod |
| Sync | auto + prune + self-heal | manual/workflow duoc duyet |
| Web replicas (KEDA min-max) | 1-2 | 2-8 |
| Worker replicas (KEDA min-max) | 1-1 | 1-4 |
| Rollout | canary theo replica, khong APISIX | APISIX traffic-weight canary + analysis |
| Access | ClusterIP dev-access, SSH port-forward | firewall HA -> private ELB -> APISIX TLS |
| Database | odoo17_dev | odoo17_prod |
| PVC / Secret | rieng namespace dev | rieng namespace prod |
| Required node label / NoSchedule taint | odoo.cmc/environment=dev | odoo.cmc/environment=prod |
| CPU request quota | 8 | 24 |
| Memory request quota | 12Gi | 32Gi |

Application Details hien thi Environment, Access, Release policy. Tat ca resource cua
overlay co label `odoo.cmc/environment`; khong doi selector cua Deployment/Service.
Khong them banner vao giao dien Odoo: day la repo GitOps, khong phai source ung dung.

## Truoc khi bat auto-sync Dev

Bootstrap Dev truoc: node labels/taints, DB/role, namespace va runtime Secrets phai san sang.
Khi root nhan thay doi Application nay, Dev co the tu sync ngay. Khong dung auto-sync
de thay the DB/NFS preflight. Khong xoa namespace, PVC, database hay legacy application.
Prod khong tu sync; trong GitHub Settings -> Environments -> odoo-prod can cau hinh
required reviewers. YAML `environment:` tu no KHONG tao approval gate.

## Demo 1: thay doi Git chi anh huong Dev

1. Mo hai Application odoo-dev/odoo-prod tren Argo. Loc theo project/label environment.
2. Trong overlays/dev/boundaries.yaml, doi ResourceQuota requests.cpu tu '8' sang '9'.
   Day la tran quota, khong doi replica hay yeu cau tai nguyen cua pod.
3. Chay validation, review, commit/push nhanh dev. Khong push thay doi demo nay vao main/prod.
4. Quan sat Dev OutOfSync -> Synced; resource quota Dev thanh 9. Prod quota van 24.
   Hai app theo doi hai nhanh dev/prod: SHA GitOps khong phai image version.
5. Revert commit demo qua Git, Dev tu ve 8. Khong sua cmc-common de demo thay doi rieng Dev.

## Demo 2: drift va self-heal (tuy chon, chi Dev)

Sau khi quota Dev da ve 8, co the chu dong tao drift nho:

```bash
kubectl -n odoo-dev patch resourcequota odoo-budget --type=merge -p '{"spec":{"hard":{"requests.cpu":"9"}}}'
kubectl -n odoo-dev get resourcequota odoo-budget -w
```

Day la ngoai le co chu dich cho demo, KHONG phai cach deploy. Argo se dua quota ve 8.
Khong scale Deployment de demo drift vi KEDA so huu replicas va Argo ignore truong do.
Khong tao drift tren Prod, DB hoac Secret.

## Demo 3: build mot lan, promote cung image

1. App CI tao immutable sha image va chi cap nhat overlays/dev. Dev tu reconcile.
2. Chay verify Dev, ghi nhan dung image tag va ket qua; xem rollout va logs.
3. Prod van giu image cu. Hai moi truong cung tag sau promotion la binh thuong.
4. Chay Promote verified Dev images to prod branch tren main voi dev_revision va expected_tag da nghiem thu.
   Workflow hien tai yeu cau ca Odoo va backup image deu co tag do; khong dien tag gia.
5. Reviewer duyet GitHub environment odoo-prod; workflow copy image pins, khong rebuild,
   roi yeu cau Argo sync. Quan sat stable/canary/AnalysisRun tren Prod.
   Workflow promotion nay sync ngay sau approval; khong ky vong no dung cho bam Sync thu cong.
6. Rollback bang Git revert image pins va sync co kiem soat; rollback image khong rollback DB schema.

## Audit placement tren bastion (read-only)

Trong checkout GitOps moi nhat, voi Python dependencies da cai:

```bash
kubectl get nodes -L odoo.cmc/environment
kubectl get nodes -o custom-columns='NAME:.metadata.name,TAINTS:.spec.taints'
python scripts/environment.py preflight --environment dev --context k8s-odoo-admin@k8s-odoo
python scripts/environment.py preflight --environment prod --context k8s-odoo-admin@k8s-odoo
kubectl -n odoo-dev get pods -o wide
kubectl -n odoo-prod get pods -o wide
```

Audit kiem tra affinity/toleration cua pod, label/taint node dang chay, eligible nodes
va metrics API. No khong thay the kiem thu network isolation, DB privileges hay endpoint.
NoSchedule khong duoi pod cu; IgnoredDuringExecution khong di chuyen pod khi doi label.
Node replacement can nhan label/taint tu node-group config, khong chi gan tay mot lan.

Snapshot user cung cap ngay 2026-10-18: 5 pod Dev nam tren hai dev workers; 5 pod Prod
nam tren hai prod workers, trong do hai web replicas nam tren hai node khac nhau.
Khong thay Odoo chay cheo nhom theo TEN node. Can lenh tren de xac minh LABEL/TAINT that.
APISIX/etcd/Argo/KEDA o nhom worker platform cu la hop ly. Calico, kube-proxy, CSI,
node-exporter tren ca dev/prod la daemon he thong can thiet, khong phai ro ri workload.
Ba etcd replicas dang nam tren hai platform workers: hai member cung node la rui ro
quorum khi mat node do; can node platform thu ba neu muon chiu mat bat ky mot node.
Khong tu chuyen etcd vao Dev/Prod hoac ep anti-affinity khi chua co du node.
