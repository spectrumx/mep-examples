#!/bin/bash
#
# This script is used to synchronize the time on the local machine
# with the time on the remote machine. It should result in a ~25ms
# difference between the local and remote time.
#
# For this script to work the user on the remote machine must 
# have sudo access without a password. 
#
# This can be done by running the following command:
# sudo visudo
#
# and adding the following to the sudoers file:
#
# mep ALL=(ALL) NOPASSWD: ALL
#
###########################################################

# Check if IP address is provided
if [ $# -eq 0 ]; then
    echo "Usage: $0 <remote_ip> [username]"
    echo "Example: $0 192.168.1.100"
    echo "Example: $0 192.168.1.100 mep"
    echo ""
    echo "This script synchronizes the time on the local machine with the time on the remote machine."
    echo "It should result in a ~25ms difference between the local and remote time."
    echo ""
    echo "For this script to work the user on the remote machine must have sudo access without a password."
    echo "This can be done by running the following command:"
    echo "  sudo visudo"
    echo ""
    echo "and adding the following to the sudoers file:"
    echo "  mep ALL=(ALL) NOPASSWD: ALL"
    exit 1
fi

REMOTE_IP="$1"
REMOTE_USER="${2:-"mep"}"  # Use provided username or current user

# Function to log messages
log_message() {
    local message="$1"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S.%3N')
    echo "[$timestamp] $message"
}

perform_time_sync() {
    local time_string="$1"
    
    log_message "Performing time synchronization for time: $time_string"
    
    ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$REMOTE_USER@$REMOTE_IP" << EOF
        # Function to log messages on remote machine
        log_remote() {
            local message="\$1"
            local timestamp=\$(date '+%Y-%m-%d %H:%M:%S.%3N')
            echo "[\$timestamp] \$message"
        }
        
        log_remote "Starting time synchronization process for time: $time_string"
        
        # Stop the time synchronization service to prevent conflicts
        log_remote "Stopping systemd-timesyncd..."
        sudo systemctl stop systemd-timesyncd 2>/dev/null || true
        
        # Set the system time using sudo with millisecond precision
        log_remote "Setting system time to: $time_string"
        sudo date -s "$time_string"
        
        # Sync the hardware clock using sudo
        log_remote "Syncing hardware clock..."
        sudo hwclock --systohc
        
        # Restart the time synchronization service
        log_remote "Restarting systemd-timesyncd..."
        sudo systemctl start systemd-timesyncd 2>/dev/null || true
        
        # Verify the time was set correctly with millisecond precision
        current_time=\$(date '+%Y-%m-%d %H:%M:%S.%3N')
        log_remote "Time synchronization completed. Current time: \$current_time"
        
        # Return the current time for verification
        echo "REMOTE_TIME:\$current_time"
EOF
    
    if [ $? -eq 0 ]; then
        log_message "SUCCESS: Time synchronized to remote machine"
        return 0
    else
        log_message "ERROR: Failed to set time on remote machine"
        return 1
    fi
}

# Function to verify time synchronization
verify_sync() {
    log_message "Verifying time synchronization..."
    
    # Get timestamps with millisecond precision
    local local_time=$(date '+%Y-%m-%d %H:%M:%S.%3N')
    local remote_time=$(ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$REMOTE_USER@$REMOTE_IP" "date '+%Y-%m-%d %H:%M:%S.%3N'")
    
    if [ $? -eq 0 ]; then
        log_message "Local time:  $local_time"
        log_message "Remote time: $remote_time"
        
        # Convert to milliseconds since epoch for precise comparison
        local local_ms=$(date -d "$local_time" +%s%3N)
        local remote_ms=$(date -d "$remote_time" +%s%3N)
        local diff_ms=$((local_ms - remote_ms))
        
        # Convert to absolute value for display
        local abs_diff_ms=${diff_ms#-}
        
        if [ $abs_diff_ms -le 50 ]; then  # Allow 50ms difference for millisecond precision
            log_message "SUCCESS: Time synchronization verified (difference: ${diff_ms}ms)"
        else
            log_message "WARNING: Time difference detected (${diff_ms}ms)"
        fi
    else
        log_message "ERROR: Could not verify remote time"
    fi
}

# Main execution
main() {
    log_message "Starting time synchronization to $REMOTE_USER@$REMOTE_IP"
    
    # Get local time with millisecond precision
    local_time=$(date '+%Y-%m-%d %H:%M:%S.%3N')
    log_message "Local machine time: $local_time"
    
    # Perform time synchronization in single SSH session
    if ! perform_time_sync "$local_time"; then
        exit 1
    fi
    
    # Verify synchronization
    verify_sync
    
    log_message "Time synchronization completed"
}

# Run main function
main "$@" 