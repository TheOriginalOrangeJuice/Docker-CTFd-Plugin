CTFd.plugin.run((_CTFd) => {
    const $ = _CTFd.lib.$;
    const md = _CTFd.lib.markdown();

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
                ports.forEach(function (port) {
                    $ports.append($("<option />").val(port).text(port).prop("selected", true));
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
                        $select.prop("disabled", true);
                        $("#dockerimage_label").text("Docker Image (Docker API is not configured correctly)");
                        return;
                    }
                    $select.append($("<option />").val(item.name).text(item.name));
                });
                $select.trigger("change");
            })
            .fail(function () {
                $("#dockerimage_select").prop("disabled", true);
                $("#dockerimage_label").text("Docker Image (failed to load images)");
            });
    }

    $('a[href="#new-desc-preview"]').on("shown.bs.tab", function (event) {
        if (event.target.hash === "#new-desc-preview") {
            const editorValue = $("#new-desc-editor").val();
            $(event.target.hash).html(md.render(editorValue));
        }
    });

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
        toggleMode($('input[name="docker_mode"]:checked').val() || "single");
        toggleFlagMode($('input[name="flag_mode"]:checked').val() || "static");
    });
});
