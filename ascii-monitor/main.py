import boto3
import os
import time
from datetime import datetime
from botocore.exceptions import ClientError

AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-2")
DYNAMO_TABLE = os.getenv("DYNAMO_TABLE", "ascii-api-job-table")

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table(DYNAMO_TABLE)

SERVICES_TO_MONITOR = [
    "ascii-api-service1",
    "ascii-converter"
]

ecs = boto3.client("ecs", region_name=AWS_REGION)
CLUSTER = "ascii-cluster-main"

def check_services():
    """Check running tasks for each ECS service."""
    statuses = []
    for service in SERVICES_TO_MONITOR:
        try:
            resp = ecs.describe_services(cluster=CLUSTER, services=[service])
            running = resp["services"][0]["runningCount"]
            desired = resp["services"][0]["desiredCount"]
            status = "✅ Healthy" if running >= desired else "⚠️ Degraded"
            statuses.append({
                "service": service,
                "running": running,
                "desired": desired,
                "status": status
            })
        except ClientError as e:
            statuses.append({
                "service": service,
                "status": f"❌ Error: {e.response['Error']['Message']}"
            })
    return statuses


def store_to_dynamodb(statuses):
    """Store the monitoring result into DynamoDB."""
    timestamp = datetime.utcnow().isoformat()
    for s in statuses:
        table.put_item(Item={
            "id": f"monitor-{s['service']}-{timestamp}",
            "service": s["service"],
            "status": s["status"],
            "timestamp": timestamp
        })


if __name__ == "__main__":
    print("🚀 Starting ASCII Monitor Service")
    print(f"📊 Monitoring ECS cluster: {CLUSTER}")

    while True:
        try:
            statuses = check_services()
            for s in statuses:
                print(f"{s['service']}: {s['status']} ({s['running']}/{s['desired']})")
            store_to_dynamodb(statuses)
            print("✅ Results stored in DynamoDB.\n")
        except Exception as e:
            print("❌ Error during monitoring:", e)

        # Check every 60 seconds
        time.sleep(60)
