CTFd._internal.challenge.data = undefined;
CTFd._internal.challenge.renderer = CTFd._internal.markdown;

let checkInterval = null;
let revertCountdownTimer = null;
let expiryCountdownTimer = null;

CTFd._internal.challenge.preRender = function () {};

CTFd._internal.challenge.render = function (markdown) {
    return CTFd._internal.challenge.renderer.parse(markdown);
};

CTFd._internal.challenge.postRender = function () {
    createWarningModalBody();
    getDockerStatus();
};

function getChallengeContext() {
    const challenge = CTFd._internal.challenge.data || {};
    return {
        challengeId: challenge.id,
        challengeName: challenge.name,
        containerName: challenge.docker_image,
    };
}

function clearTimers() {
    if (revertCountdownTimer) {
        clearInterval(revertCountdownTimer);
        revertCountdownTimer = null;
    }
    if (expiryCountdownTimer) {
        clearInterval(expiryCountdownTimer);
        expiryCountdownTimer = null;
    }
}

function createWarningModalBody() {
    if (CTFd.lib.$("#warningModalBody").length === 0) {
        CTFd.lib.$("body").append('<div id="warningModalBody"></div>');
    }
}

function formatDuration(totalSeconds) {
    const seconds = Math.max(0, Math.floor(totalSeconds));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainingSeconds = seconds % 60;

    if (hours > 0) {
        return `${hours}h ${minutes}m`;
    }
    if (minutes > 0) {
        return `${minutes}m ${remainingSeconds}s`;
    }
    return `${remainingSeconds}s`;
}

function createPortRows(item) {
    const services = item.services || [];
    const lines = [];

    services.forEach(service => {
        const servicePorts = service.ports || [];
        if (servicePorts.length === 0) {
            return;
        }

        servicePorts.forEach(port => {
            const cleanPort = String(port).split("/")[0];
            if (item.is_compose) {
                lines.push(
                    `<strong>${CTFd.lib.$("<div>").text(service.service_name).html()}</strong>: ` +
                    `${item.host}:${cleanPort}`
                );
            } else {
                lines.push(`${item.host}:${cleanPort}`);
            }
        });
    });

    return lines;
}

function renderConnectionInfo(item) {
    const firstPort = ((item.ports || [])[0] || "").split("/")[0];

    CTFd.lib.$(".challenge-connection-info").each(function () {
        const $element = CTFd.lib.$(this);
        if (!$element.data("dockerTemplate")) {
            $element.data("dockerTemplate", $element.html());
        }

        let html = $element.data("dockerTemplate");
        if (html.includes("{{HOST}}") || html.includes("{{PORT}}")) {
            html = html.replace(/\{\{HOST\}\}/g, item.host || "");
            html = html.replace(/\{\{PORT\}\}/g, firstPort || "");
        } else {
            html = html.replace(/\bhost\b/gi, item.host || "");
            html = html.replace(/\bport\b/gi, firstPort || "");
        }

        const urlMatch = html.match(/(http[s]?:\/\/[^\s<]+)/);
        if (urlMatch && !html.includes("<a ")) {
            const url = urlMatch[0];
            html = html.replace(
                url,
                `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`
            );
        }

        $element.html(html);
    });
}

function renderContainerActions(item, containerName) {
    const $actionArea = CTFd.lib.$("#docker_container_actions");
    const cooldownEndsAt = new Date(parseInt(item.revert_time, 10) * 1000).getTime();
    const stopButton =
        `<a onclick="stop_container('${containerName}');" class="btn btn-dark">` +
        `<small style="color:white;"><i class="fas fa-stop"></i> Stop</small></a>`;

    const renderReadyActions = () => {
        $actionArea.html(
            `<a onclick="start_container('${containerName}');" class="btn btn-dark">` +
            `<small style="color:white;"><i class="fas fa-redo"></i> Revert</small></a> ${stopButton}`
        );
    };

    const now = Date.now();
    if (now >= cooldownEndsAt) {
        renderReadyActions();
        return;
    }

    $actionArea.html(`${stopButton} <span class="revert-countdown ml-2"></span>`);
    revertCountdownTimer = setInterval(function () {
        const remainingMs = cooldownEndsAt - Date.now();
        if (remainingMs <= 0) {
            clearInterval(revertCountdownTimer);
            revertCountdownTimer = null;
            renderReadyActions();
            return;
        }
        $actionArea.find(".revert-countdown").text(`Revert in ${formatDuration(remainingMs / 1000)}`);
    }, 1000);
}

function renderExpiry(item) {
    const $expiry = CTFd.lib.$("#docker_container_expiry");
    if (!$expiry.length || !item.expires_at) {
        return;
    }

    const expiresAt = new Date(parseInt(item.expires_at, 10) * 1000).getTime();
    const updateExpiryText = () => {
        const remainingMs = expiresAt - Date.now();
        if (remainingMs <= 0) {
            clearInterval(expiryCountdownTimer);
            expiryCountdownTimer = null;
            $expiry.text("Instance expired. Refresh to request a new one.");
            return;
        }
        $expiry.text(`Auto-stops in ${formatDuration(remainingMs / 1000)}`);
    };

    updateExpiryText();
    expiryCountdownTimer = setInterval(updateExpiryText, 1000);
}

function renderStoppedState(containerName) {
    clearTimers();
    CTFd.lib.$("#docker_container").html(
        `<span><a onclick="start_container('${containerName}');" class="btn btn-dark">` +
        `<small style="color:white;"><i class="fas fa-play"></i> Start Docker Instance for challenge</small>` +
        `</a></span>`
    );
}

function getDockerStatus() {
    const context = getChallengeContext();
    if (!context.challengeId) {
        return;
    }

    CTFd.fetch("/api/v1/docker_status")
        .then(response => response.json())
        .then(result => {
            const item = (result.data || []).find(entry => {
                if (entry.challenge_id !== null && entry.challenge_id !== undefined) {
                    return parseInt(entry.challenge_id, 10) === parseInt(context.challengeId, 10);
                }
                return entry.challenge === context.challengeName;
            });

            if (!item) {
                renderStoppedState(context.containerName);
                return;
            }

            clearTimers();
            renderConnectionInfo(item);

            const portRows = createPortRows(item);
            const infoHtml = portRows.length
                ? portRows.map(line => `${line}<br />`).join("")
                : "Container is running but does not publish any ports.";

            CTFd.lib.$("#docker_container").html(
                `<pre>Docker Instance Information:<br />${infoHtml}</pre>` +
                `<div class="small text-muted mb-2" id="docker_container_expiry"></div>` +
                `<div class="mt-2" id="docker_container_actions"></div>`
            );

            renderContainerActions(item, context.containerName);
            renderExpiry(item);
        })
        .catch(error => {
            console.error("Error fetching docker status:", error);
        });
}

function stop_container(containerName) {
    const context = getChallengeContext();
    if (!confirm(`Stop the Docker instance for:\n${context.challengeName}`)) {
        return;
    }

    CTFd.fetch("/api/v1/container", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
        },
        body: JSON.stringify({
            name: containerName,
            challenge: context.challengeName,
            challenge_id: context.challengeId,
            stopcontainer: true,
        }),
    })
        .then(response => response.json().then(json => ({ ok: response.ok, json })))
        .then(({ ok, json }) => {
            if (!ok) {
                throw new Error(json.message || "Failed to stop container");
            }
            updateWarningModal({
                title: "Attention!",
                warningText:
                    "The Docker instance for <br><strong>" +
                    context.challengeName +
                    "</strong><br> was stopped successfully.",
                buttonText: "Close",
                onClose: function () {
                    getDockerStatus();
                },
            });
        })
        .catch(error => {
            updateWarningModal({
                title: "Error",
                warningText: error.message || "An unknown error occurred while stopping the container.",
                buttonText: "Close",
                onClose: function () {
                    getDockerStatus();
                },
            });
        });
}

function start_container(containerName) {
    const context = getChallengeContext();
    CTFd.lib.$("#docker_container").html(
        '<div class="text-center"><i class="fas fa-circle-notch fa-spin fa-1x"></i></div>'
    );

    CTFd.fetch("/api/v1/container", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
        },
        body: JSON.stringify({
            name: containerName,
            challenge: context.challengeName,
            challenge_id: context.challengeId,
        }),
    })
        .then(response => response.json().then(json => ({ ok: response.ok, json })))
        .then(({ ok, json }) => {
            if (!ok) {
                throw new Error(json.message || "Failed to start container");
            }

            getDockerStatus();
            updateWarningModal({
                title: "Attention!",
                warningText:
                    "A Docker instance has been started for you.<br>" +
                    "Use the connection information above to access it.",
                buttonText: "Got it!",
            });
        })
        .catch(error => {
            updateWarningModal({
                title: "Error!",
                warningText: error.message || "An unknown error occurred when starting your Docker container.",
                buttonText: "Got it!",
                onClose: function () {
                    getDockerStatus();
                },
            });
        });
}

function updateWarningModal({ title, warningText, buttonText, onClose } = {}) {
    const modalHTML = `
        <div id="warningModal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; z-index:9999; background-color:rgba(0,0,0,0.5);">
          <div style="position:relative; margin:10% auto; width:400px; background:white; border-radius:8px; box-shadow:0 2px 10px rgba(0,0,0,0.3); overflow:hidden;">
            <div class="modal-header bg-warning text-dark" style="padding:1rem; display:flex; justify-content:space-between; align-items:center;">
              <h5 class="modal-title" style="margin:0;">${title}</h5>
              <button type="button" id="warningCloseBtn" style="border:none; background:none; font-size:1.5rem; line-height:1; cursor:pointer;">&times;</button>
            </div>
            <div class="modal-body" style="padding:1rem;">
              ${warningText}
            </div>
            <div class="modal-footer" style="padding:1rem; text-align:right; border-top:1px solid #dee2e6;">
              <button type="button" class="btn btn-secondary" id="warningOkBtn">${buttonText}</button>
            </div>
          </div>
        </div>
    `;
    CTFd.lib.$("#warningModalBody").html(modalHTML);
    CTFd.lib.$("#warningModal").show();

    const closeModal = () => {
        CTFd.lib.$("#warningModal").hide();
        if (typeof onClose === "function") {
            onClose();
        }
    };

    CTFd.lib.$("#warningCloseBtn").on("click", closeModal);
    CTFd.lib.$("#warningOkBtn").on("click", closeModal);
}

function checkForCorrectFlag() {
    const challengeWindow = document.querySelector("#challenge-window");
    if (!challengeWindow || getComputedStyle(challengeWindow).display === "none") {
        clearInterval(checkInterval);
        checkInterval = null;
        return;
    }

    const notification = document.querySelector(".notification-row .alert");
    if (!notification) {
        return;
    }

    const strong = notification.querySelector("strong");
    if (!strong) {
        return;
    }

    if (strong.textContent.trim().includes("Correct")) {
        getDockerStatus();
        clearInterval(checkInterval);
        checkInterval = null;
    }
}

if (!checkInterval) {
    checkInterval = setInterval(checkForCorrectFlag, 1500);
}
