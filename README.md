# BirdNET-Go weekly digest

A weekly email from your BirdNET-Go station, built to look like part of the BirdNET-Go dashboard. It reads the station's `/api/v2` API, picks out what was interesting about the week and sends it through Mailgun. Nothing on the Pi needs mail configuration.

What's in each email:

- **Week at a glance:** detections, species, new species and the busiest day, with change against the previous week
- **Highlight of the week:** the best first-ever species, or the week's rarest regular, with its spectrogram
- **New arrivals:** species heard for the first time ever, or for the first time this year
- **Daily activity:** detections per day
- **Who sang when:** a species-by-hour heatmap in the same colour ramp as the dashboard's daily summary
- **Top species:** the ten busiest, with change against their four-week average
- **After dark:** species detected between sunset and sunrise
- **Gone quiet:** regulars from the past month with no detections this week

Detections you've marked as false positives are left out. Light and dark mode both follow the BirdNET-Go palette, and `COLOR_SCHEME` matches whichever scheme you use in the app.

## Install on the Pi

```sh
git clone https://github.com/ptpittman/birdnet-email.git ~/birdnet-email
cd ~/birdnet-email
cp .env.example .env
nano .env        # Mailgun key, domain, from/to, station name, timezone
```

Check the Pi can see BirdNET-Go:

```sh
curl -s http://localhost:8080/api/v2/health
```

Then:

```sh
./run.sh test      # short setup-check email: Mailgun works, station reachable
./run.sh preview   # writes out/preview.html, sends nothing
./run.sh now       # send this week's digest now
./run.sh start     # build and start the weekly scheduler
./run.sh logs      # watch it
```

`now` and `test` take `--to someone@example.com` to send somewhere other than `MAIL_TO`. `now` and `preview` take `--end 2026-10-25` to report the seven days ending on a given date.

## Updating

```sh
cd ~/birdnet-email
./run.sh update
```

This pulls the latest code, rebuilds the image and restarts the scheduler. Your `.env` stays put because git ignores it.

## Running it without the script

`run.sh` wraps plain Docker commands, so Docker Compose isn't required. The equivalents are:

```sh
docker build -t birdnet-digest .
docker run --rm --network host --env-file .env birdnet-digest --now
docker run -d --name birdnet-digest --restart unless-stopped --network host --env-file .env birdnet-digest
```

If you do have the Compose plugin, `docker compose up -d --build` works too.

## Mailgun

You need an API key (Mailgun > API Security) and a sending domain. If the domain was created in Mailgun's EU region, set `MAILGUN_REGION=eu`. `MAIL_FROM` has to be an address on that domain.

## Notes

- **Links.** Buttons in the email open `BIRDNET_PUBLIC_URL`. If you reach the Pi through Tailscale or a reverse proxy, put that address here so links work away from home.
- **Private Mode.** BirdNET-Go's read endpoints are public by default. If you've turned on authentication or Private Mode, allow the Pi's own network through with `security.allowsubnetbypass` in BirdNET-Go's config. Its bearer tokens expire hourly, so a token is a poor fit for a weekly job.
- **Images.** Bird photos and the spectrogram are fetched from BirdNET-Go and embedded in the email, so they display even though the Pi isn't reachable from the internet. Photo credits come from BirdNET-Go's image providers and are listed in the footer.
