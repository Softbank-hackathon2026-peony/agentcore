#!/bin/sh
# REAL_IP_FROM(공백으로 구분한 CIDR 목록)을 CIDR마다 set_real_ip_from 한 줄로 바꾼다.
# 템플릿의 ${REAL_IP_FROM}에 그대로 넣으면 CIDR가 두 개 이상일 때 nginx 문법 오류가 난다.
# /etc/nginx/conf.d는 nginx-unprivileged 이미지에서 nginx 사용자(uid 101)가 쓸 수 있다.
# 확장자가 .conf가 아니므로 conf.d/*.conf로 자동 포함되지 않고, 템플릿의 include로만 읽힌다.
set -eu
out=/etc/nginx/conf.d/real-ip.inc
: > "$out"
for cidr in ${REAL_IP_FROM:-}; do
  printf 'set_real_ip_from %s;\n' "$cidr" >> "$out"
done
echo "$0: wrote $(wc -l < "$out") set_real_ip_from line(s) to $out"
