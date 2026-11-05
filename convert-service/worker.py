import boto3
import json
import os
from PIL import Image
import numpy as np
from datetime import datetime

# === AWS clients ===
region = "ap-southeast-2"
sqs = boto3.client("sqs", region_name=region)
s3 = boto3.client("s3", region_name=region)
dynamodb = boto3.resource("dynamodb", region_name=region)
ssm = boto3.client("ssm", region_name=region)

# === Load queue URL and DynamoDB table name from SSM Parameters ===
queue_url = ssm.get_parameter(Name="/ascii-api/queue-url")["Parameter"]["Value"]
table_name = ssm.get_parameter(Name="/ascii-api/table-name")["Parameter"]["Value"]
table = dynamodb.Table(table_name)

# === ASCII converter function ===
def convert_image_to_ascii(image_path, output_path):
    chars = "@%#*+=-:. "
    image = Image.open(image_path).convert("L")
    image = image.resize((100, 50))
    arr = np.array(image)

    ascii_art = "\n".join(
        "".join([chars[min(pixel // 25, len(chars) - 1)] for pixel in row]) for row in arr
    )

    with open(output_path, "w") as f:
        f.write(ascii_art)

# === Worker polling loop ===
print("🛠️ Worker started. Polling for messages...")

while True:
    try:
        response = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20
        )

        messages = response.get("Messages", [])
        if not messages:
            continue

        for msg in messages:
            try:
                body = json.loads(msg["Body"])

                username = body["username"]
                input_key = body["s3_input_key"]
                output_key = body["s3_output_key"]
                original_filename = body.get("original_filename", "unknown")

                bucket = "ascii-api-video-store"
                input_path = "/tmp/input.png"
                output_path = "/tmp/output.txt"

                # Download image from S3
                s3.download_file(bucket, input_key, input_path)

                # Convert to ASCII
                convert_image_to_ascii(input_path, output_path)

                # Upload result to S3
                s3.upload_file(output_path, bucket, output_key)

                # Update DynamoDB
                table.update_item(
                    Key={"qut-username": username, "filename": original_filename},
                    UpdateExpression="SET #s = :val",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":val": "done"},
                )

                # Delete message from queue
                sqs.delete_message(
                    QueueUrl=queue_url,
                    ReceiptHandle=msg["ReceiptHandle"]
                )

                print(f"✅ Success: {input_key} → {output_key}")

            except Exception as err:
                print(f"❌ Error processing message: {err}")

    except Exception as poll_error:
        print(f"⚠️ Polling error: {poll_error}")
