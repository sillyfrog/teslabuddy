FROM python

RUN pip install --upgrade pip

WORKDIR /app
ENV  PYTHONUNBUFFERED=1

COPY requirements.txt /tmp/
RUN pip install --requirement /tmp/requirements.txt

COPY *.py /app/

CMD ["/app/teslabuddy.py"]