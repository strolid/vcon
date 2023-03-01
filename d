#!/bin/bash

if [ "$1" == "start" ]; then
  sudo docker-compose --profile dev up -d
elif [ "$1" == "bash" ]; then
  sudo docker-compose exec conserver bash
elif [ "$1" == "restart" ]; then
  sudo docker-compose --profile dev restart conserver
elif [ "$1" == "build" ]; then
  sudo docker-compose --profile dev build conserver
else
  echo "Usage: ./d start   # to start docker compose"
  echo "       ./d bash    # to run bash inside the 'conserver' container"
  echo "       ./d restart # to restart 'conserver' container"
  echo "       ./d build   # to build 'conserver' container"
fi
