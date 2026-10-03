#!/usr/bin/env bash
set -e

echo "=== Building JARs for all backend microservices ==="
for svc in snippet-service snippet-permission-service snippet-runner-service snippet-asset-service; do
  echo "--> Building $svc..."
  (cd "../$svc" && ./gradlew bootJar -x test)
done

echo "=== All backend JARs successfully built! ==="
