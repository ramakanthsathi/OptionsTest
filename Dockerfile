# Paper trader container. Runs one trading day per execution (see deploy/azure.sh for the cron job).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=America/New_York
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY aziz_options_scanner.py datasources.py catalyst.py paper_trader.py publish.py journal_report.py ./
# No ca_bundle.pem here: the AVG TLS interception only exists on the dev machine.
# Secrets come from the environment: UW_API_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY,
# JOURNAL_BLOB_SAS_URL, JOURNAL_PAGE_PASSPHRASE.  Extra args via TRADER_ARGS.
CMD ["sh", "-c", "python paper_trader.py --wait-until 09:40 ${TRADER_ARGS}"]
