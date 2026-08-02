#!/bin/bash

# Prompt the user for the commit message
echo -n "Enter your commit message: "
read -r commit_message

# Check if the message is empty
if [ -z "$commit_message" ]; then
    echo "Error: Commit message cannot be empty!"
    exit 1
fi

# Run the git commands
git commit -am "$commit_message" && git push

