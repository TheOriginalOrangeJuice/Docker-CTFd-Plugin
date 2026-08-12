CTFd.plugin.run((_CTFd) => {
    const $ = _CTFd.lib.$;

    function toggleMode(mode) {
        const composeMode = mode === "compose";
        $("#single_image_group").toggle(!composeMode);
        $("#single_ports_group").toggle(!composeMode);
        $("#compose_group").toggle(composeMode);
        $("#dockerimage_select").prop("required", !composeMode);
        $("#docker_ports_select").prop("required", !composeMode);
        if (!composeMode) {
            $("#compose_content").val("");
        }
    }

    function loadDockerPorts(image) {
        const $ports = $("#docker_ports_select");
        $ports.empty().prop("disabled", true);
        if (!image) {
            return;
        }
        $.getJSON("/api/v1/docker?image=" + encodeURIComponent(image))
            .done(function (result) {
                const ports = (result.data && result.data.ports) || [];
                let selected = PUBLISHED_PORTS;
                if (typeof selected === "string") {
                    try {
                        selected = JSON.parse(selected);
                    } catch (error) {
                        selected = [];
                    }
                }
                if (!Array.isArray(selected) || selected.length === 0) {
                    selected = ports;
                }
                if (image !== DOCKER_IMAGE) {
                    selected = ports;
                }
                ports.forEach(function (port) {
                    $ports.append(
                        $("<option />").val(port).text(port).prop("selected", selected.includes(port))
                    );
                });
                $ports.prop("disabled", ports.length === 0);
            })
            .fail(function () {
                $ports.append($("<option />").text("Failed to inspect image ports").prop("disabled", true));
            });
    }

    function toggleFlagMode(mode) {
        const useHmac = mode === "hmac";
        $("#flag_template_group").toggle(useHmac);
        $("#flag_template").prop("required", useHmac);
    }

    function loadDockerImages() {
        $.getJSON("/api/v1/docker")
            .done(function (result) {
                const images = result.data || [];
                const $select = $("#dockerimage_select");
                $select.empty();

                images.forEach(function (item) {
                    if (item.name === "Error in Docker Config!") {
                        $("#dockerimage_label").text("Docker Image (Docker API is not configured correctly)");
                        return;
                    }
                    $select.append($("<option />").val(item.name).text(item.name));
                });

                if (DOCKER_IMAGE && !$select.find(`option[value="${DOCKER_IMAGE}"]`).length && DOCKER_IMAGE !== "compose") {
                    $select.append($("<option />").val(DOCKER_IMAGE).text(DOCKER_IMAGE));
                }
                if (DOCKER_IMAGE && DOCKER_IMAGE !== "compose") {
                    $select.val(DOCKER_IMAGE).trigger("change");
                }
            })
            .fail(function () {
                $("#dockerimage_label").text("Docker Image (failed to load images)");
            });
    }

    $(document).ready(function () {
        $('[data-toggle="tooltip"]').tooltip();
        loadDockerImages();
        $("#dockerimage_select").on("change", function () {
            loadDockerPorts(this.value);
        });

        $('input[name="docker_mode"]').on("change", function () {
            toggleMode(this.value);
        });
        $('input[name="flag_mode"]').on("change", function () {
            toggleFlagMode(this.value);
        });

        toggleMode(COMPOSE_CONTENT ? "compose" : "single");
        toggleFlagMode(FLAG_MODE || "static");
    });
});
