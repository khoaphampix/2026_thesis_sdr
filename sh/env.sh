 #!/usr/bin/env bash

# Initialize pyenv and virtualenv management
eval "$(pyenv init -)"
eval "$(pyenv virtualenv-init -)"

# Activate the target environment
pyenv activate pysdr_3_11_9