set -xe

echo "[**] Bao installer"

# create user
sudo adduser --disabled-password --gecos 'PaaS access' bao
# copy your public key to /tmp (assuming it's the first entry in authorized_keys)
sudo su - bao -c 'mkdir -p ~/.ssh'
head -1 ~/.ssh/authorized_keys | sudo tee /home/bao/.ssh/authorized_keys
sudo chown bao:bao /home/bao/.ssh/authorized_keys
# install bao
sudo cp bao.py /home/bao
sudo chmod +x /home/bao/bao.py
sudo chown bao:bao /home/bao/bao.py
sudo su - bao -c 'curl -sSL https://install.python-poetry.org | python3 -'
/home/bao/bao.py init

echo "[==] installed :)"
