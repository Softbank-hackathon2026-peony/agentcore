# Cloud Run 아키텍처 견본 (Pawploy 작성, Worker 모듈과 같은 입력·출력 규격)
# 공통 입력: name, image_uri, container_port, size, env, health_path
# 공통 출력: endpoint, health_url, resource_id
# 라벨(pawploy-*)·리전·프로젝트는 루트의 google provider(default_labels)가 강제한다.

terraform {
  required_providers {
    google = {
      source = "hashicorp/google"
    }
  }
}

variable "name" { type = string }
variable "image_uri" { type = string }
variable "container_port" { type = number }
variable "size" { type = string }
variable "env" { type = map(string) }
variable "health_path" { type = string }

locals {
  memory = {
    micro  = "512Mi"
    small  = "1Gi"
    medium = "2Gi"
  }
}

resource "google_cloud_run_v2_service" "app" {
  name                = var.name
  location            = "asia-northeast3" # 서울
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false # 1시간 뒤 destroy 가 막히지 않게

  template {
    scaling {
      max_instance_count = 1 # 악용 방지
    }
    containers {
      image = var.image_uri
      ports {
        container_port = var.container_port # Cloud Run 이 PORT 환경변수를 이 값으로 넣어준다
      }
      resources {
        limits = {
          cpu    = "1"
          memory = local.memory[var.size]
        }
      }
      dynamic "env" {
        for_each = var.env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }
}

# 인증 없이 누구나 접속 (테스트 배포용)
resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.app.name
  location = google_cloud_run_v2_service.app.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "endpoint" {
  value = google_cloud_run_v2_service.app.uri
}

output "health_url" {
  value = "${google_cloud_run_v2_service.app.uri}${var.health_path}"
}

output "resource_id" {
  value = google_cloud_run_v2_service.app.id
}
