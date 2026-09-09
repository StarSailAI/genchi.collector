FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/browser/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && python -m playwright install-deps firefox \
    && apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 browser

USER browser
# Match SkillHub's F route. Fetch at build time; never update binaries at startup.
ENV CAMOUFOX_BROWSER=official/152.0.4-beta.30
# SDK fingerprint generation consults the active install even with browser= set.
RUN python -m camoufox fetch official/152.0.4-beta.30 \
    && python -m camoufox set official/stable/152.0.4-beta.30 \
    && python -c "from camoufox.pkgman import installed_verstr; assert installed_verstr().startswith('152.0.4-')"
COPY --chown=browser:browser services/browser/*.py /app/
CMD ["python", "/app/service.py"]
