// Descarga por fetch en vez de dejar que el navegador descargue la respuesta del POST.
//
// Algunos navegadores, al recibir un attachment como respuesta a un POST, reintentan
// la descarga con un GET sobre la misma URL. Aquí ese GET devuelve 405 y la descarga
// se pierde, porque el JSON solo existe en la respuesta del POST: no se guarda nada.
// Con el blob la descarga ocurre sin segunda petición.
//
// Es mejora progresiva: sin JavaScript el formulario sigue enviándose de forma normal.
(function () {
  "use strict";

  var formulario = document.querySelector("form");
  if (!formulario || typeof URL.createObjectURL !== "function") return;

  function nombreDe(cabecera, porDefecto) {
    var coincidencia = /filename="([^"]+)"/.exec(cabecera || "");
    return coincidencia ? coincidencia[1] : porDefecto;
  }

  function descargar(blob, nombre) {
    var url = URL.createObjectURL(blob);
    var enlace = document.createElement("a");
    enlace.href = url;
    enlace.download = nombre;
    document.body.appendChild(enlace);
    enlace.click();
    enlace.remove();
    // Revocar de inmediato cancela la descarga en algunos navegadores.
    setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 60000);
  }

  formulario.addEventListener("submit", function (evento) {
    evento.preventDefault();

    var boton = formulario.querySelector('button[type="submit"]');
    var textoOriginal = boton.textContent;
    boton.disabled = true;
    boton.textContent = "Convirtiendo…";

    fetch(formulario.action, { method: "POST", body: new FormData(formulario) })
      .then(function (respuesta) {
        if (!respuesta.ok) {
          // Mismo resultado que sin JavaScript: se muestra la página de error.
          return respuesta.text().then(function (html) {
            document.documentElement.innerHTML = html;
          });
        }
        var nombre = nombreDe(
          respuesta.headers.get("content-disposition"),
          "pdf2json.json"
        );
        return respuesta.blob().then(function (blob) {
          descargar(blob, nombre);
        });
      })
      .catch(function (error) {
        alert("No se pudo convertir: " + error.message);
      })
      .finally(function () {
        boton.disabled = false;
        boton.textContent = textoOriginal;
      });
  });
})();
