# Hiking-from-SuisseMobile

Vous permet d'enregistrer l'itinéraire sur les montres connectées (Coros, Garmin, ...) à partir du fichier GPX dont vous trouverez la liste dans les dossiers `hiking/*`.

Chaque itinéraire :
- est numéroté selon les numéros de randonnée et de segment,
- est augmenté de waypoints à chaque kilomètre parcouru, permettant d'évaluer votre rythme
- a sa version "à revers" où les points de départ et d'arrivée sont inversés,
- a son nom raccourci pour tenir sur l'affichage de la montre,
  
Par exemple:
| Dossier | Nom fichier | Randonnée | Segment | Commentaire |
| --- | --- | --- | :---: | --- |
| `hiking/1-19/` | `1.11 Grindelwald - Lauterbrunnen.gpx` | 1 = randonnée nationale Via Alpina | 11 | Itinéraire officiel |
| `hiking/1-19/` | `1.11r Lauterbrunnen - Grindelwald.gpx` | 1 = randonnée nationale Via Alpina | 11 | Itinéraire inversé |

Consultez toujours le site https://schweizmobil.ch/ pour connaitre l'état actuel de la randonnée que vous visez.
