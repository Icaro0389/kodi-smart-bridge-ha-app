ARG BUILD_FROM
FROM ${BUILD_FROM}
RUN apk add --no-cache python3
COPY app/ /app/
COPY run.sh /run.sh
RUN chmod 755 /run.sh
CMD ["/run.sh"]
