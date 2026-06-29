FROM docker.io/n8nio/n8n:latest

# ضبط المتغيرات الأساسية لضمان استقرار جلسة العمل
ENV N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS=false
ENV N8N_ENCRYPTION_KEY=mysecretkey12345

# فتح المنفذ الافتراضي الذي يعتمد عليه Railway تلقائياً
EXPOSE 5678
