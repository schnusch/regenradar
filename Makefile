DOCKER = docker

.PHONY: image-id out/dresden.mp4

out/dresden.mp4: image-id
	mkdir -p $(@D)
	chmod 1777 $(@D)
	$(DOCKER) run -v ./$(@D):/$(@D) $$(cat image-id) regenradar -o /$@ --center=51.0515487,13.7470293 --radius=100000 --start=-1:30 --duration=3:15

image-id:
	$(DOCKER) build --iidfile $@ .
